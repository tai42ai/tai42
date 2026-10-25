"""The MCP-connection-resilience incident chain, end to end against the real
fastmcp/mcp stack.

A manifest ``mcp`` entry binds to a managed FastMCP server the test owns as a
killable http subprocess (``tai42_e2e_fixtures.managed_mcp_server --transport
http``). Every tool call is dispatched through the product's own ``/mcp`` surface,
so it rides the kit's pooled ``FastMCPClient`` and the skeleton's tool-dispatch
seam. One incident chain exercises the pooled client's dead-session eviction
predicate, the seam's one-shot reconnect, the structured ``upstream_mcp_unavailable``
result, the passive health block, and the per-title reload eviction across four
upstream states:

a. the server is up → a tool call succeeds and the passive health block on
   ``GET /api/mcp-status`` records a ``last_success``.
b. the server is SIGKILLed and restarted → the NEXT call still SUCCEEDS: the kit
   evicts the dead pooled session on the real disconnect shape and the seam's
   one-shot reconnect masks the death. This pins the eviction predicate against
   the raise shapes real fastmcp/httpx produce, not a hand-mocked exception.
c. the server is SIGKILLed and kept down → the call returns the structured
   connector-envelope error (code ``upstream_mcp_unavailable``, the entry title
   in the consumer-facing message, and NO raw fastmcp/httpx internals leaked into
   it); health shows a ``failing_since`` and ``consecutive_failures`` ≥ 1.
d. the server is restarted and the per-title reload op is run → the next call
   succeeds: the operator reload deterministically evicts the pooled session and
   health recovers.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Iterator

import pytest

from tai42_e2e import Infra, diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.managed_mcp import ManagedHttpMcpServer
from tai42_e2e.manifests import (
    RESILIENCE_MCP_TITLE,
    build_resilience_mcp_stack,
    resilience_mcp_tool_name,
)
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("process", "helper:managed-mcp-server", "topology:single-worker")

# The connector-error envelope prefix the dispatch seam frames an unavailable-upstream
# result with (``ConnectorAdapterSettings.error_prefix``); the JSON payload follows it.
_CONNECTOR_ERROR_PREFIX = "tai-hub-err:"

# The structured code the seam stamps when a pooled MCP session died and the one-shot
# reconnect could not rebuild it.
_UPSTREAM_MCP_UNAVAILABLE_CODE = "upstream_mcp_unavailable"

# Substrings that would betray raw transport/SDK internals leaking into the
# consumer-facing message — the envelope must name the entry, never these.
_RAW_INTERNALS_MARKERS = (
    "httpx",
    "anyio",
    "fastmcp",
    "Traceback",
    "RuntimeError",
    "ClosedResource",
    "BrokenResource",
    "ConnectError",
    "Session terminated",
    "ClientConnectError",
    "ClientDisconnectedError",
)

_PING_TOOL = resilience_mcp_tool_name("ping")


@pytest.fixture
def resilience_mcp_stack(
    infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, ManagedHttpMcpServer]]:
    """MULTIWORKER(1), auth off — the managed MCP server mounted over http, yielding
    ``(stack, server)``. The server is launched and confirmed serving BEFORE boot so
    the app's boot-time MCP loader binds its tools; the test drives the server's
    kill/restart through the yielded handle. Function-scoped: the scenario mutates the
    server's liveness and the passive health store, so each run starts clean."""
    server = ManagedHttpMcpServer(tmp_path_factory.mktemp("managed-mcp-http"))
    server.start()
    asyncio.run(server.wait_serving())
    try:
        root = tmp_path_factory.mktemp("mcp-resilience")
        builder = functools.partial(build_resilience_mcp_stack, mcp_url=server.url)
        resources, config = allocate_and_build(infra, root, builder, None, False)
        stack = TaiStack(config, infra, resources, root)
        with stack, diagnostics.track(stack):
            yield stack, server
    finally:
        server.stop()


async def _call_ping(stack: TaiStack, *, raise_on_error: bool = True):
    async with stack.mcp(port=stack.port_a) as mcp:
        return await mcp.call_tool(_PING_TOOL, {}, raise_on_error=raise_on_error, retry_on_reloading=True)


async def _health(stack: TaiStack) -> dict:
    status = await stack.api().get("/api/mcp-status", retry_on_reloading=True)
    health = status["health"]
    assert RESILIENCE_MCP_TITLE in health, f"no health block for {RESILIENCE_MCP_TITLE!r}: {status}"
    return health[RESILIENCE_MCP_TITLE]


def _collect_strings(obj: object) -> list[str]:
    """Every string reachable in a nested result payload. The seam's error result
    surfaces through the app's ``/mcp`` as a tool result whose content wraps the
    framed connector envelope one level down, so the envelope string is found by
    walking the whole structure rather than assuming a flat content block."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for value in obj.values() for s in _collect_strings(value)]
    if isinstance(obj, list):
        return [s for value in obj for s in _collect_strings(value)]
    return []


def _framed_error_payload(result) -> dict | None:
    """The connector-error payload framed as ``<prefix><json>`` anywhere in the
    result, or ``None`` if the result carries no envelope (a success). The prefix is
    stripped and the remainder — a complete JSON object — is parsed."""
    texts = _collect_strings(result.data) + [getattr(part, "text", "") for part in result.content]
    framed = next((t for t in texts if _CONNECTOR_ERROR_PREFIX in t), None)
    if framed is None:
        return None
    return json.loads(framed[framed.index(_CONNECTOR_ERROR_PREFIX) + len(_CONNECTOR_ERROR_PREFIX) :])


async def test_mcp_connection_resilience_incident_chain(
    resilience_mcp_stack: tuple[TaiStack, ManagedHttpMcpServer],
) -> None:
    stack, server = resilience_mcp_stack

    # (a) The managed tool bound at boot; a call succeeds and health records a success.
    async with stack.mcp(port=stack.port_a) as mcp:
        names = await mcp.tool_names()
    assert _PING_TOOL in names, f"{_PING_TOOL!r} not mounted from the managed http MCP: {sorted(names)}"

    result = await _call_ping(stack)
    assert not result.is_error, f"first ping returned an error: {result.content!r}"
    health = await _health(stack)
    assert health["last_success"] is not None, f"no last_success after a good call: {health}"
    assert health["consecutive_failures"] == 0, f"a failure run opened on a good call: {health}"

    # (b) Self-heal: SIGKILL then restart the server; the pooled session is now dead,
    # so the next dispatch must evict it on the real disconnect shape and the one-shot
    # reconnect must rebind against the fresh server — the call SUCCEEDS.
    server.restart()
    await server.wait_serving()
    healed = await _call_ping(stack)
    assert not healed.is_error, f"ping did not self-heal after a restart: {healed.content!r}"
    healed_texts = _collect_strings(healed.data) + [getattr(part, "text", "") for part in healed.content]
    assert "pong" in healed_texts, f"the healed ping did not carry the pong payload: {healed_texts!r}"
    assert not any(_CONNECTOR_ERROR_PREFIX in text for text in healed_texts), (
        f"a connector-error envelope surfaced in a supposedly healed ping: {healed_texts!r}"
    )
    health = await _health(stack)
    assert health["consecutive_failures"] == 0, f"self-heal left a failure run open: {health}"

    # (c) Hard down: SIGKILL and keep the server dead. The dispatch loses its session,
    # the reconnect cannot rebuild it, and the seam returns the structured envelope.
    server.kill()
    down = await _call_ping(stack, raise_on_error=False)
    payload = _framed_error_payload(down)
    assert payload is not None, f"a call to a dead upstream returned no connector envelope: {down!r}"
    assert payload["code"] == _UPSTREAM_MCP_UNAVAILABLE_CODE, f"unexpected error code: {payload}"
    message = payload["message"]
    assert RESILIENCE_MCP_TITLE in message, f"the entry title is not named in the message: {message!r}"
    for marker in _RAW_INTERNALS_MARKERS:
        assert marker not in message, f"raw internals {marker!r} leaked into the consumer message: {message!r}"

    health = await _health(stack)
    assert health["failing_since"] is not None, f"no failing_since while the upstream is down: {health}"
    assert health["consecutive_failures"] >= 1, f"consecutive_failures did not advance: {health}"
    assert health["last_error"] is not None, f"no last_error recorded: {health}"
    assert set(health["last_error"]) == {"type", "message", "at"}, f"last_error shape wrong: {health['last_error']}"

    # (d) Operator reload heals: restart the server, run the per-title reload op (which
    # deterministically evicts the pooled session), then the next call succeeds and
    # health recovers.
    server.start()
    await server.wait_serving()
    reload_report = await stack.api().post(
        f"/api/mcp-status/{RESILIENCE_MCP_TITLE}/reload", json={}, retry_on_reloading=True
    )
    assert reload_report["reachable"] is True, f"the reload broadcast was unreachable: {reload_report}"
    assert all(r["outcome"] == "applied" for r in reload_report["results"]), f"a worker did not apply: {reload_report}"

    recovered = await _call_ping(stack)
    assert not recovered.is_error, f"ping did not recover after reload: {recovered.content!r}"
    health = await _health(stack)
    assert health["last_success"] is not None, f"no last_success after recovery: {health}"
    assert health["consecutive_failures"] == 0, f"the failure run did not close after recovery: {health}"
    assert health["last_error"] is None, f"last_error not cleared after recovery: {health}"
