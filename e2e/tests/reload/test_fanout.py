"""C9 — the worker-bus confirmed broadcast. A reload reaches every live worker (both
HTTP replicas and the backend runtime) and each confirms ``applied``; a dispatch with a
dead worker still on the presence census names it with a non-applied outcome — never a
silent drop — and every survivor confirms once the corpse's presence row expires. Workers
are named by their stable slot name on the census and in the fan-out report alike."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import build_replicas_stack
from tai42_e2e.stack import Infra, TaiStack
from tai42_e2e.variants import short_presence_ttl_env


async def test_reload_config_fans_out_confirmed(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    marker = uniq("E2E_MARKER").upper()
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    # Seed a config change (polling past the boot-time reload gate), then fan the reload.
    await api_a.post("/api/config/env", json={marker: "1"}, retry_on_reloading=True)

    async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
        # Prime the SDK's per-session tool-output-schema cache so the post-call result
        # validation issues no follow-up tools/list: reload_config retires this session
        # (D13a new session-id space), which would otherwise raise "Session terminated".
        await mcp.list_tools()
        result = await mcp.call_tool("reload_config", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"reload_config returned no result map: {result!r}"
    # A FleetResult (no ``workers`` key): reachable, one per-worker outcome each.
    assert data["op"] == "reload_config"
    assert data["reachable"] is True, f"the bus was unreachable: {data}"
    outcomes = {r["name"]: r["outcome"] for r in data["results"]}
    # Every census worker (the backend worker + each HTTP worker) confirmed applied.
    census = {worker.name for worker in replicas_stack.census()}
    assert census <= outcomes.keys(), f"not every census worker was in the report: census={census} report={outcomes}"
    assert all(outcomes[name] == "applied" for name in census), f"a worker did not apply: {data}"

    # B's subscription applied the op too (asynchronous via the admin route).
    async def b_has_marker() -> bool:
        env = (await replicas_stack.api(port=replicas_stack.port_b).get("/api/config/env"))["env"]
        return marker in env

    await wait_for_async(b_has_marker, deadline=5.0, message="B never observed the reloaded config marker")


async def test_dispatch_names_dead_worker(fresh_stack: Callable[..., TaiStack], infra: Infra) -> None:
    # Presence and its TTL live on the app-owned bus, so the dead-worker window is tunable
    # for every backend. Two knobs are pinned here:
    #  * a bounded ``TAI_BUS_APPLY_TIMEOUT`` so a reload's wait for the DEAD backend's apply
    #    is cut short (naming it non-applied) instead of running to the 30s default — on
    #    celery that long wait holds the driver's own streamable-http response until it
    #    races the epoch-swap teardown and never arrives (an unbounded client hang); arq/rq
    #    deliver fast, which is why only celery hung. Mirrors ``test_backend_fleet``.
    #  * a generous heartbeat TTL so the corpse stays on the census long enough that a
    #    re-opened session (after a swap that dropped a prior attempt) still finds it there
    #    to name — the dead-worker naming is what this test asserts.
    stack = fresh_stack(
        build_replicas_stack, env_overrides={**short_presence_ttl_env(25), "TAI_BUS_APPLY_TIMEOUT": "5"}
    )

    # Drain worker A's boot-time self-resync gate first, so the dispatch below is not
    # itself rejected as reloading while the corpse's presence-TTL counts down.
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.call_tool("e2e_worker_info", retry_on_reloading=True)

    # The backend worker's bus slot name, matched by kind + pid off the presence census.
    dead_pid = stack.process("backend").pid
    dead_name = next(w.name for w in stack.census() if w.kind == "backend" and w.pid == dead_pid)

    # Kill the backend, then dispatch the reload while the corpse still sits on the census
    # (the generous TTL keeps it there across any session-swap re-open). The reload names
    # the dead worker non-applied. The pinned ``TAI_BUS_APPLY_TIMEOUT`` keeps the reload's
    # wait for the dead backend's apply UNDER the server's deferred session-close budget, so
    # the driving call's own streamable-http response is delivered before the swap retires
    # its session; ``McpClient`` re-initialises transparently for any genuine mid-call
    # termination. The prime skips the post-call ``tools/list`` on the retired session.
    stack.kill("backend")
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.list_tools()
        result = await mcp.call_tool("reload_config", {}, raise_on_error=False, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"reload_config returned no result map: {result!r}"
    # The dead worker is NAMED with a non-applied outcome (departed/missing), never a
    # silently dropped worker the report pretends converged.
    outcomes = {r["name"]: r["outcome"] for r in data["results"]}
    assert dead_name in outcomes, f"the dead backend was not named in the report: {data}"
    assert outcomes[dead_name] != "applied", f"the dead backend was reported as applied: {data}"

    # Once its presence row expires (kill -9 = no graceful delete, so it lapses on the TTL),
    # the surviving HTTP workers all confirm applied.
    async def census_dropped_backend() -> bool:
        return not any(worker.pid == dead_pid for worker in stack.census())

    await wait_for_async(census_dropped_backend, deadline=35.0, message="dead worker never left the census")
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.list_tools()
        ok = await mcp.call_tool("reload_config", {}, retry_on_reloading=True)
    ok_data = ok.data if isinstance(ok.data, dict) else ok.structured_content
    assert isinstance(ok_data, dict), f"reload_config returned no result map: {ok!r}"
    assert all(r["outcome"] == "applied" for r in ok_data["results"]), f"a survivor did not apply: {ok_data}"
