"""A stored preset keeps its name over an MCP tool served under the same name, on every replica.

The managed http MCP server is the ``acme`` entry on a two-replica fleet and is down at
boot. The preset ``acme_ping`` (over the probe tool ``e2e_echo``) is created while nothing
serves that name; the server then starts and ``acme`` is reloaded, and replica B is
restarted so it binds from a fresh boot. Every replica runs the preset under the name and
reports the server's ``acme_ping`` as unbound for a preset conflict, whatever the order of
boots and binds. Deleting the preset and reloading ``acme`` hands the name to the MCP tool
on both replicas.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.managed_mcp import ManagedHttpMcpServer, replicas_mcp_stack
from tai42_e2e.manifests import REPLICAS_MCP_TITLE, replicas_mcp_tool_name
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("process", "helper:managed-mcp-server", "topology:replicas", "mutable")

_NAME = replicas_mcp_tool_name("ping")
_PRESET_PAYLOAD = "preset-base-ran"
_ADVERTISED = f"/api/mcp-status/{REPLICAS_MCP_TITLE}/advertised-tools"

# How long a reload on one replica may take to land on the other (the fleet fan-out
# re-probes the server and rebinds).
_REBIND_S = 60.0


@pytest.fixture
def fleet(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TaiStack, ManagedHttpMcpServer]]:
    server = ManagedHttpMcpServer(tmp_path_factory.mktemp("preset-mcp-name-mcp"))
    try:
        with replicas_mcp_stack(infra, tmp_path_factory.mktemp("preset-mcp-name"), server) as stack:
            yield stack, server
    finally:
        server.stop()


async def _row(stack: TaiStack, port: int) -> dict:
    body = await stack.api(port=port).post(_ADVERTISED, retry_on_reloading=True)
    return next(row for row in body["tools"] if row["name"] == _NAME)


async def _run(stack: TaiStack, port: int) -> str:
    outcome = await stack.api(port=port).post(
        "/api/run-tool", json={"tool_name": _NAME, "arguments": {}}, retry_on_reloading=True
    )
    assert outcome["kind"] == "result", outcome
    return json.dumps(outcome["result"])


async def _wait_row(stack: TaiStack, *, bound: bool, reason: str | None) -> None:
    for port in (stack.port_a, stack.port_b):

        async def settled(port: int = port) -> bool:
            row = await _row(stack, port)
            return row["bound"] is bound and row["unbound_reason"] == reason

        await wait_for_async(
            settled, deadline=_REBIND_S, message=f"replica on {port} never read {_NAME} bound={bound} reason={reason}"
        )


@pytest.mark.timeout(480)
async def test_a_stored_preset_keeps_its_name_over_a_served_mcp_tool_on_every_replica(
    fleet: tuple[TaiStack, ManagedHttpMcpServer],
) -> None:
    stack, server = fleet
    api_a = stack.api(port=stack.port_a)

    await api_a.post(
        "/api/presets",
        json={
            "name": _NAME,
            "base_tool": "e2e_echo",
            "description": "echo preset under an MCP tool name",
            "fixed_kwargs": {"payload": _PRESET_PAYLOAD},
        },
        retry_on_reloading=True,
    )

    server.start()
    await server.wait_serving()
    report = await api_a.post(f"/api/mcp-status/{REPLICAS_MCP_TITLE}/reload", json={}, retry_on_reloading=True)
    assert report["reachable"] is True, report
    stack.restart("serve-b")

    await _wait_row(stack, bound=False, reason="preset_conflict")
    for port in (stack.port_a, stack.port_b):
        presets = await stack.api(port=port).get("/api/presets", retry_on_reloading=True)
        row = next(item for item in presets if item["name"] == _NAME)
        assert row["conflicted"] is False, row
        result = await _run(stack, port)
        assert _PRESET_PAYLOAD in result, result
        assert "pong" not in result, result

    # The remedy the row names: delete the preset, reload the server; the MCP tool takes the name.
    await api_a.delete(f"/api/presets/{_NAME}", retry_on_reloading=True)
    report = await api_a.post(f"/api/mcp-status/{REPLICAS_MCP_TITLE}/reload", json={}, retry_on_reloading=True)
    assert report["reachable"] is True, report

    await _wait_row(stack, bound=True, reason=None)
    for port in (stack.port_a, stack.port_b):
        result = await _run(stack, port)
        assert "pong" in result, result
