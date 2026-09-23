"""The MCP ``tools/call`` edge carries a caller-named ``subject`` in the request ``_meta``.

The edge reads ``_meta["tai42/subject"]`` and deposits the same ``door="api"`` state context the
direct run-tool doors deposit, so a tool whose ``ask(mode="async")`` parks over MCP indexes under
the named subject. A malformed subject is refused loudly at the edge, never silently dropped.

Over ``replicas_stack`` (the app ``/mcp`` edge, the async-park probe tools, access control off):
the probe ``e2e_async_park_flow`` dispatched over MCP with a subject in ``_meta`` parks a user ask;
``list_parked`` on the SAME subject (read through the run-tool HTTP door) lists that park, and on a
DIFFERENT subject lists nothing. A ``_meta`` subject that cannot validate errors the call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.stack import TaiStack

_PARK_EXPIRY_SECONDS = 3600.0


def _subject(key: str) -> dict[str, str]:
    return {"target_kind": "agent", "target_name": "a-42", "kind": "job", "key": key}


async def _parked_ids(api: Any, subject: dict[str, str]) -> list[str]:
    """The ids ``list_parked`` reports for ``subject``, read through the run-tool HTTP door which
    deposits that subject's context — the same interaction store the MCP edge parked into."""
    entries = await api.post(
        "/api/run-tool",
        json={"tool_name": "list_parked", "arguments": {}, "subject": subject},
        expect=200,
        retry_on_reloading=True,
    )
    return [entry["id"] for entry in entries]


async def test_mcp_call_edge_carries_subject(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    subject = _subject(uniq("subj"))
    other = _subject(uniq("other"))
    marker = uniq("mcp-park")

    async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
        result = await mcp.call_tool(
            "e2e_async_park_flow",
            {"question": marker, "expiry_seconds": _PARK_EXPIRY_SECONDS},
            meta={"tai42/subject": subject},
            retry_on_reloading=True,
        )
    interaction_id = result.data["interaction_id"]

    # The MCP edge indexed the park under the named subject; that subject lists it, another does not.
    assert interaction_id in await _parked_ids(api, subject)
    assert interaction_id not in await _parked_ids(api, other)


async def test_mcp_malformed_subject_meta_is_refused(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    marker = uniq("mcp-bad")
    async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
        # A partial subject (no target) can never validate — the edge raises rather than
        # dropping the subject and running the tool with no context.
        result = await mcp.call_tool(
            "e2e_async_park_flow",
            {"question": marker, "expiry_seconds": _PARK_EXPIRY_SECONDS},
            meta={"tai42/subject": {"kind": "job", "key": "j1"}},
            raise_on_error=False,
            retry_on_reloading=True,
        )
    assert result.is_error, result
