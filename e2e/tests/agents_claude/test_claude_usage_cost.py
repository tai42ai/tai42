"""§3e — the ``claude_code`` model-cost emission (the billing enabler's claude half).

``claude_code`` drives the Claude Agent SDK INSIDE the sandbox, so its model calls bypass the
platform ``get_llm_async`` meter entirely. To stay billable it emits the SDK-reported
usage/cost off the terminal ``result`` frame into the active trace as a generation span's
cost fields (``Span.update(usage_details=...)``). The fixture monitoring backend records that
emission on ``e2e:rec:span_cost``, and the stack names ``E2E_MONITOR_ACTIVE_TRACE`` so the
cost-emission guard (an active trace) is satisfied on this backend-less stack.

Contrast (asserted structurally by the record's presence here, and directly in the deep suite):
``langchain_deep_agent`` needs NO such emission — its model turn runs SERVER-side through the
metered ``get_llm_async``, so the normal meter already bills it. The two halves are: this
claude path EMITS, the deep path RELIES ON THE METER.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._claude_support import claude_stack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("claude_agent"),
        reason="scripted runner stub is the 'claude_agent' mock leg; the real turn is the §5 smoke",
    ),
]


async def test_sdk_reported_usage_is_emitted_as_span_cost(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "usage")

    # The fixture span-cost probe list is shared across the session, so measure the delta this
    # one run adds rather than an absolute count.
    before = len(stack.records("span_cost"))

    async with stack.mcp() as mcp:
        result = await mcp.call_tool("claude_code", {"user_message": "hi"}, retry_on_reloading=True)
    assert result.data == "counted", result.data

    # The adapter emitted the SDK usage/cost into the active trace DURING the drive (before the
    # terminal answer the MCP call awaited), so the fixture backend has already recorded it on
    # ``e2e:rec:span_cost`` as exactly ONE new generation-span cost record by the time it returns.
    added = stack.records("span_cost")[before:]
    assert len(added) == 1, added
    record = json.loads(added[0])
    assert record["kind"] == "LLM", record
    assert record["usage_details"] == {"input_tokens": 12, "output_tokens": 34, "total_cost_usd": 0.0042}, record
