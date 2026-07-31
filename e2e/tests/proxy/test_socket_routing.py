"""C8 — the proxy extension flips ``socket.socket`` process-wide. A leak is
invisible single-process, so these assert "exactly N CONNECTs" and a pristine
socket class under real concurrency: proxied and plain calls in flight at once."""

from __future__ import annotations

import asyncio

from tai42_e2e import wait_for_async
from tai42_e2e.netfixtures import RecordingConnectProxy, TargetServer
from tai42_e2e.stack import TaiStack


async def test_proxy_branch_tunnels_and_plain_does_not(
    core_stack: TaiStack, target_server: TargetServer, connect_proxy: RecordingConnectProxy
) -> None:
    targets_before = len(target_server.records)
    connects_before = len(connect_proxy.records)

    async with core_stack.mcp() as mcp:
        # A reload-gate rejection is refused before the tool runs (no HTTP request), so
        # polling past a boot-time ``reloading`` never perturbs the CONNECT counts below.
        proxied = await mcp.call_tool(
            "e2e_http_probe_proxy",
            {"url": f"{target_server.url}/ok", "proxies": [connect_proxy.url]},
            retry_on_reloading=True,
        )
    assert proxied.data["status"] == 200
    assert len(target_server.records) == targets_before + 1
    assert len(connect_proxy.records) == connects_before + 1, "proxied call did not go through the CONNECT proxy"

    async with core_stack.mcp() as mcp:
        plain = await mcp.call_tool("e2e_http_probe", {"url": f"{target_server.url}/ok"}, retry_on_reloading=True)
    assert plain.data["status"] == 200
    assert len(target_server.records) == targets_before + 2
    # The plain call must NOT touch the proxy.
    assert len(connect_proxy.records) == connects_before + 1


async def test_no_cross_task_socket_bleed_under_interleaving(
    core_stack: TaiStack, target_server: TargetServer, connect_proxy: RecordingConnectProxy
) -> None:
    connects_before = len(connect_proxy.records)

    async def proxied() -> int:
        async with core_stack.mcp() as mcp:
            result = await mcp.call_tool(
                "e2e_http_probe_proxy",
                {"url": f"{target_server.url}/slow?ms=500", "proxies": [connect_proxy.url]},
                retry_on_reloading=True,
            )
        return result.data["status"]

    async def plain() -> int:
        async with core_stack.mcp() as mcp:
            result = await mcp.call_tool("e2e_http_probe", {"url": f"{target_server.url}/ok"}, retry_on_reloading=True)
        return result.data["status"]

    results = await asyncio.gather(*([proxied() for _ in range(4)] + [plain() for _ in range(4)]))
    assert all(status == 200 for status in results), f"a call failed under interleaving: {results}"
    # Exactly 4 CONNECTs — a 5th means a plain call leaked onto the proxy route.
    # This is THE cross-task-bleed guard: routing is dispatched per asyncio task
    # on a contextvar, so a proxied task's route can never bleed into a concurrent
    # plain task's sockets. A leak would tunnel a plain call and push this over 4.
    assert len(connect_proxy.records) == connects_before + 4

    # Task-scoped routing installs the RoutingSocket dispatcher on ``socket.socket``
    # once and leaves it there (transparent when no route is active on the task) —
    # it is NOT uninstalled after each call. So the dispatcher stays installed
    # across every worker; the per-task contextvar, asserted end-to-end by the
    # CONNECT count above, is what keeps concurrent calls isolated.
    async with core_stack.mcp() as mcp:
        for _ in range(6):
            info = await mcp.call_tool("e2e_worker_info", retry_on_reloading=True)
            assert info.data["socket_class"].endswith(".RoutingSocket"), f"proxy dispatcher not installed: {info.data}"


async def test_no_cross_replica_bleed(
    replicas_stack: TaiStack, target_server: TargetServer, connect_proxy: RecordingConnectProxy
) -> None:
    connects_before = len(connect_proxy.records)

    async def long_proxied_on_a() -> None:
        async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
            await mcp.call_tool(
                "e2e_http_probe_proxy",
                {"url": f"{target_server.url}/slow?ms=1000", "proxies": [connect_proxy.url]},
                retry_on_reloading=True,
            )

    task = asyncio.create_task(long_proxied_on_a())
    try:
        # While A holds a proxied call, plain probes on B must not tunnel.
        async def a_connected() -> bool:
            return len(connect_proxy.records) == connects_before + 1

        await wait_for_async(a_connected, deadline=3.0, message="A's proxied CONNECT never registered")
        async with replicas_stack.mcp(port=replicas_stack.port_b) as mcp:
            for _ in range(4):
                result = await mcp.call_tool(
                    "e2e_http_probe", {"url": f"{target_server.url}/ok"}, retry_on_reloading=True
                )
                assert result.data["status"] == 200
        assert len(connect_proxy.records) == connects_before + 1, "a plain B call bled through A's proxy"
    finally:
        await asyncio.wait_for(task, timeout=5.0)
