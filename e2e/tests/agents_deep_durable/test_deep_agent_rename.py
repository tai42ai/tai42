"""The ``deep_agent`` -> ``langchain_deep_agent`` rename is complete at the running SUT.

Per the no-removal-pinning rule this asserts the POSITIVE ``langchain_deep_agent``
registration (its run tool binds over ``/mcp`` and its SSE run route serves a turn) plus the
plain unknown-name MISS for the old id (``mcp.call_tool`` errors, the SSE route 404s) — never
a bespoke "stays absent" pin. The old id resolving to NOTHING is the clean break (no compat
alias): the model of a real client that still names the old agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

from ._support import AGENT, DETERMINISTIC_MARKS, run_sse, run_sse_frames

pytestmark = DETERMINISTIC_MARKS

# The old id, referenced ONLY here as the rename negative — the exact name a client that
# predates the rename would call, which must now resolve to nothing.
_OLD_ID = "deep" + "_agent"


async def test_langchain_deep_agent_binds_and_old_id_is_unknown_over_mcp(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """``langchain_deep_agent`` is a live registered run tool over ``/mcp``; the old id is not a
    tool at all (an unknown-tool error, never a silently-aliased hit)."""
    async with deep_agent_durable_stack.mcp(port=deep_agent_durable_stack.port_a) as mcp:
        names = await mcp.tool_names()
        assert AGENT in names, f"the renamed run tool did not bind over /mcp: {names}"
        assert _OLD_ID not in names, f"the old agent id is still bound as a tool: {names}"

        # Calling the old id is a plain unknown-tool miss — returned as an error result, never a
        # silent alias onto the renamed agent.
        miss = await mcp.call_tool(_OLD_ID, {"user_message": uniq("q")}, raise_on_error=False)
        assert miss.is_error, f"the old agent id resolved instead of missing: {miss.data!r}"


async def test_langchain_deep_agent_runs_and_old_id_route_404s_over_sse(
    deep_agent_durable_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """The renamed agent's SSE run route serves a scripted turn; the old id's SSE run route
    404s (no such registered agent)."""
    final = f"renamed-serves {uniq('run')}"
    llm_stub.reset()
    llm_stub.script([{"content": final}])

    port = deep_agent_durable_stack.port_a
    frames = await run_sse_frames(
        deep_agent_durable_stack, port, f"/api/agents/{AGENT}/runs", {"user_message": uniq("hello")}
    )
    assert any(final in frame for frame in frames), f"the renamed agent SSE never delivered its final: {frames}"

    # The old id names no registered agent, so its run route is a 404 — not a 5xx, not a
    # silently-served turn.
    old = await run_sse(deep_agent_durable_stack, port, f"/api/agents/{_OLD_ID}/runs", {"user_message": uniq("q")})
    assert old.status_code == 404, f"the old agent id SSE route did not 404: {old.status_code} {old.text}"
    assert _OLD_ID in json.dumps(old.text) or "not found" in old.text.lower(), old.text
