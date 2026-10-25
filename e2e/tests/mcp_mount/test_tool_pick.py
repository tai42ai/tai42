"""An MCP server's tool pick, end to end on a two-replica fleet.

The managed http MCP server is mounted as the ``acme`` entry on both replicas. The
advertised-tools read lists every tool the server advertises with whether the entry picks
it and whether the answering replica binds it; the tool-filter write records the pick in
the manifest, and every replica rebinds to it, so an unpicked tool leaves ``GET /api/tools``
on both. A pick that would stop mounting a tool a stored preset is built over is refused
with a 409 naming the preset, and the tool stays bound on both replicas.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

import pytest

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.managed_mcp import ManagedHttpMcpServer, replicas_mcp_stack
from tai42_e2e.manifests import REPLICAS_MCP_TITLE, replicas_mcp_tool_name
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("process", "helper:managed-mcp-server", "topology:replicas", "mutable")

_PING = replicas_mcp_tool_name("ping")
_ECHO = replicas_mcp_tool_name("echo")
_REFLECT = replicas_mcp_tool_name("reflect_env")
_GONE = replicas_mcp_tool_name("gone")
_ADVERTISED = f"/api/mcp-status/{REPLICAS_MCP_TITLE}/advertised-tools"
_FILTER = f"/api/mcp-config/entries/{REPLICAS_MCP_TITLE}/tool-filter"

# How long a pick written on one replica may take to be bound on the other: the write's
# fleet fan-out reloads the sibling, which re-probes the server and rebinds.
_REBIND_S = 60.0


@pytest.fixture
def stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    server = ManagedHttpMcpServer(tmp_path_factory.mktemp("tool-pick-mcp"))
    server.start()
    try:
        asyncio.run(server.wait_serving())
        with replicas_mcp_stack(infra, tmp_path_factory.mktemp("tool-pick"), server) as booted:
            yield booted
    finally:
        server.stop()


def _ports(stack: TaiStack) -> tuple[int, int]:
    return stack.port_a, stack.port_b


async def _rows(stack: TaiStack, port: int) -> dict:
    return await stack.api(port=port).post(_ADVERTISED, retry_on_reloading=True)


async def _wait_rows(stack: TaiStack, expected: dict[str, tuple[bool, bool]], unadvertised: list[str]) -> None:
    """Wait until both replicas read each name's ``(selected, bound)`` and the unadvertised list."""
    for port in _ports(stack):

        async def settled(port: int = port) -> dict | None:
            body = await _rows(stack, port)
            seen = {row["name"]: (row["selected"], row["bound"]) for row in body["tools"]}
            matches = all(seen.get(name) == pair for name, pair in expected.items())
            return body if matches and body["unadvertised"] == unadvertised else None

        await wait_for_async(settled, deadline=_REBIND_S, message=f"replica on {port} never read {expected}")


async def _wait_tools(stack: TaiStack, present: set[str], absent: set[str]) -> None:
    """Wait until ``GET /api/tools`` on both replicas carries ``present`` and none of ``absent``."""
    for port in _ports(stack):

        async def settled(port: int = port) -> bool:
            names = set(await stack.api(port=port).get("/api/tools", retry_on_reloading=True))
            return present <= names and not (absent & names)

        await wait_for_async(
            settled, deadline=_REBIND_S, message=f"replica on {port} never listed {present} without {absent}"
        )


@pytest.mark.timeout(420)
async def test_a_pick_is_read_written_and_bound_on_every_replica(stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = stack.api(port=stack.port_a)

    every = {_PING: (True, True), _ECHO: (True, True), _REFLECT: (True, True)}
    await _wait_rows(stack, every, [])
    body = await _rows(stack, stack.port_b)
    assert body["title"] == REPLICAS_MCP_TITLE
    assert (body["include"], body["exclude"]) == ([], [])
    assert all(row["unbound_reason"] is None for row in body["tools"]), body

    # An exclude pick: the excluded tool is unpicked and unbound on both replicas.
    await api_a.put(_FILTER, json={"include": [], "exclude": [_REFLECT]}, retry_on_reloading=True)
    await _wait_rows(stack, {_PING: (True, True), _ECHO: (True, True), _REFLECT: (False, False)}, [])
    await _wait_tools(stack, present={_PING, _ECHO}, absent={_REFLECT})

    # A preset over a picked tool holds it: an include pick that drops it is refused, naming
    # the preset, and the tool stays bound on both replicas.
    preset = uniq("pickheld")
    await api_a.post(
        "/api/presets",
        json={
            "name": preset,
            "base_tool": _ECHO,
            "description": "preset over a picked MCP tool",
            "fixed_kwargs": {"payload": "held"},
        },
        retry_on_reloading=True,
    )
    refused = await api_a.request_raw("PUT", _FILTER, json={"include": [_PING], "exclude": []})
    assert refused.status_code == 409, refused.text
    payload = refused.json()
    assert f"cannot unpick {_ECHO!r}" in payload["error"], payload
    assert f"preset {preset!r}, which uses {_ECHO!r}" in payload["error"], payload
    assert [(holder["kind"], holder["name"]) for holder in payload["holders"]] == [("preset", preset)], payload
    await _wait_tools(stack, present={_PING, _ECHO}, absent={_REFLECT})
    for port in _ports(stack):
        stored = await stack.api(port=port).get("/api/manifest", retry_on_reloading=True)
        entry = next(item for item in stored["mcp"] if item["title"] == REPLICAS_MCP_TITLE)
        assert (entry.get("include", []), entry.get("exclude", [])) == ([], [_REFLECT]), entry

    # With the preset gone the include pick lands; a listed name the server does not
    # advertise reads as unadvertised.
    await api_a.delete(f"/api/presets/{preset}", retry_on_reloading=True)
    await api_a.put(_FILTER, json={"include": [_PING, _GONE], "exclude": []}, retry_on_reloading=True)
    await _wait_rows(stack, {_PING: (True, True), _ECHO: (False, False), _REFLECT: (False, False)}, [_GONE])
    await _wait_tools(stack, present={_PING}, absent={_ECHO, _REFLECT})

    # Both lists empty mounts every tool again.
    await api_a.put(_FILTER, json={"include": [], "exclude": []}, retry_on_reloading=True)
    await _wait_rows(stack, every, [])
    await _wait_tools(stack, present={_PING, _ECHO, _REFLECT}, absent=set())
