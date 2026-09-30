"""How a mounted MCP's dispatch failures reach the caller — as a structured tool-error result,
never a raw 500, on both the run-tool HTTP door and the MCP tools/call edge.

A manifest ``mcp`` entry binds to the low-level ``misbehaving_mcp_server`` fixture over stdio.
Its tools misbehave in the three shapes a downstream MCP can inflict on the dispatch seam:

* ``bad_schema`` — the upstream returns structured content that violates its own advertised
  output schema; the calling MCP SDK rejects it with a plain ``RuntimeError``.
* ``hang`` — the upstream never answers, so the per-call dispatch times out (``McpError``).
* ``plain_text`` — the upstream returns a non-JSON text body with no output schema; this is a
  well-formed result, NOT a dispatch failure.

Both failure shapes are surfaced as the SAME connector-envelope tool-error result (code
``mcp_dispatch_failed``) that names the tool and the entry title and leaks NO raw SDK/schema
internals — the identical mechanism the auth-blocked and upstream-unavailable paths use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tai42_e2e import Infra
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_misbehaving_mcp_stack, misbehaving_mcp_tool_name
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("setting:manifest-mcp-misbehaving")

# The connector-error envelope prefix every dispatch-seam tool-error result is framed with
# (``ConnectorAdapterSettings.error_prefix``); the JSON payload follows it.
_CONNECTOR_ERROR_PREFIX = "tai-hub-err:"
_DISPATCH_FAILED_CODE = "mcp_dispatch_failed"

# Substrings that would betray raw transport/SDK/schema-validator internals leaking into the
# consumer-facing message — the envelope must name the tool and entry, never these.
_RAW_INTERNALS_MARKERS = (
    "Invalid structured content",
    "jsonschema",
    "Timed out",
    "ClientRequest",
    "Traceback",
    "RuntimeError",
    "McpError",
    "httpx",
    "anyio",
    "fastmcp",
)

_BAD_SCHEMA_TOOL = misbehaving_mcp_tool_name("bad_schema")
_HANG_TOOL = misbehaving_mcp_tool_name("hang")
_PLAIN_TEXT_TOOL = misbehaving_mcp_tool_name("plain_text")
_OK_TOOL = misbehaving_mcp_tool_name("ok")


@pytest.fixture(scope="module")
def misbehaving_mcp_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """MULTIWORKER(1), auth off — the low-level misbehaving MCP server mounted over stdio, its
    per-call dispatch timeout lowered so the ``hang`` tool trips it within the test's patience."""
    yield from boot_stack(infra, tmp_path_factory.mktemp("mcp-misbehaving"), build_misbehaving_mcp_stack)


def _collect_strings(obj: object) -> list[str]:
    """Every string reachable in a nested result payload — the framed envelope surfaces one
    level down inside the tool result's content, so walk the whole structure."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for value in obj.values() for s in _collect_strings(value)]
    if isinstance(obj, list):
        return [s for value in obj for s in _collect_strings(value)]
    return []


def _framed_payload(strings: list[str]) -> dict | None:
    """The connector-error payload framed as ``<prefix><json>`` anywhere in ``strings``, or
    ``None`` when none carries the envelope. The prefix is stripped and the JSON parsed."""
    framed = next((t for t in strings if _CONNECTOR_ERROR_PREFIX in t), None)
    if framed is None:
        return None
    return json.loads(framed[framed.index(_CONNECTOR_ERROR_PREFIX) + len(_CONNECTOR_ERROR_PREFIX) :])


def _assert_dispatch_failed(payload: dict | None, tool: str) -> None:
    assert payload is not None, "no connector-error envelope surfaced for a dispatch failure"
    assert payload["code"] == _DISPATCH_FAILED_CODE, f"unexpected error code: {payload}"
    message = payload["message"]
    assert tool.split("_", 1)[1] in message, f"the tool is not named in the message: {message!r}"
    for marker in _RAW_INTERNALS_MARKERS:
        assert marker not in message, f"raw internals {marker!r} leaked into the message: {message!r}"


async def _run_tool_http(stack: TaiStack, tool: str):
    """Drive one tool through the run-tool HTTP door, returning the raw response so the status
    and body are inspectable."""
    return await stack.api().request_raw("POST", "/api/run-tool", json={"tool_name": tool, "arguments": {}})


async def test_misbehaving_tools_are_mounted(misbehaving_mcp_stack: TaiStack) -> None:
    async with misbehaving_mcp_stack.mcp(port=misbehaving_mcp_stack.port_a) as mcp:
        names = await mcp.tool_names()
    for tool in (_OK_TOOL, _BAD_SCHEMA_TOOL, _HANG_TOOL, _PLAIN_TEXT_TOOL):
        assert tool in names, f"{tool!r} not mounted from the misbehaving MCP: {sorted(names)}"


async def test_healthy_tool_round_trips(misbehaving_mcp_stack: TaiStack) -> None:
    data = await misbehaving_mcp_stack.api().post("/api/run-tool", json={"tool_name": _OK_TOOL, "arguments": {}})
    assert data == {"ok": True}, f"the healthy tool did not round trip: {data!r}"


async def test_plain_text_is_not_a_failure(misbehaving_mcp_stack: TaiStack) -> None:
    resp = await _run_tool_http(misbehaving_mcp_stack, _PLAIN_TEXT_TOOL)
    assert resp.status_code == 200, f"a non-JSON text body should round trip, not fail: {resp.status_code} {resp.text}"
    assert "this is not json" in resp.text, f"the verbatim text was not returned: {resp.text}"
    assert _CONNECTOR_ERROR_PREFIX not in resp.text, f"a healthy text result carried an error envelope: {resp.text}"


@pytest.mark.needs("setting:TAI_MCP_CALL_TIMEOUT_SECONDS=2")
@pytest.mark.parametrize("tool", [_BAD_SCHEMA_TOOL, _HANG_TOOL])
async def test_dispatch_failure_typed_on_http_door(misbehaving_mcp_stack: TaiStack, tool: str) -> None:
    # The sync run-tool HTTP door: a dispatch failure is a 200 carrying the structured tool-error
    # result the caller can read, never a 500 leaking the SDK's message.
    resp = await _run_tool_http(misbehaving_mcp_stack, tool)
    assert resp.status_code == 200, f"a typed tool-error result must not be a 500: {resp.status_code} {resp.text}"
    _assert_dispatch_failed(_framed_payload(_collect_strings(resp.json())), tool)


@pytest.mark.needs("setting:TAI_MCP_CALL_TIMEOUT_SECONDS=2")
@pytest.mark.parametrize("tool", [_BAD_SCHEMA_TOOL, _HANG_TOOL])
async def test_dispatch_failure_typed_on_mcp_edge(misbehaving_mcp_stack: TaiStack, tool: str) -> None:
    # The MCP tools/call edge: the same structured tool-error result surfaces through the app's
    # own ``/mcp`` (read with ``raise_on_error=False`` so the framed envelope is inspectable).
    async with misbehaving_mcp_stack.mcp(port=misbehaving_mcp_stack.port_a) as mcp:
        result = await mcp.call_tool(tool, {}, raise_on_error=False)
    strings = _collect_strings(result.data) + [getattr(part, "text", "") for part in result.content]
    _assert_dispatch_failed(_framed_payload(strings), tool)
