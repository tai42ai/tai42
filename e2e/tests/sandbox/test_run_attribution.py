"""Run attribution lands on the run's OWN recorded span, not merely on the wrap.

The shared run chokepoint (``ToolBinding.run_tool``) wraps a drive in ``attribute_run`` so
the run's spans open INSIDE the ambient ``RunAttribution`` scope. The fixture monitoring
backend records every opened span STAMPED with the active attribution, so a spec proves the
span fell inside the scope — not merely that the scope was entered. A one-shot design that
enters and exits the scope before the span opens would leave the span un-attributed and FAIL
these tests.

The reachable span opener under the fixture backend is the ``claude_code`` adapter's
generation span (the monitor extension SUPPRESSES itself while a trace is active, which the
attribution knob keeps it — so it cannot be the opener here). So the attribution probe
deposits a ``RunAttribution`` and drives a scripted ``claude_code`` turn whose ``result``
frame carries usage: the adapter opens the generation span inside the deposited scope, and
the recorder shows whether it was stamped. Backend-invariant, so ``backendless``.

The probe records DB is append-only and never flushed between runs, so every leg reads only
the spans it appended (a length diff) and correlates by a unique attribution tag.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_claude_agent_stack
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

# The scripted runner whose terminal ``result`` frame carries SDK usage/cost — so the adapter
# opens its generation span (the reachable in-scope span opener).
_USAGE_RUNNER = "stub:usage"

# The span the ``claude_code`` adapter opens for the SDK-reported usage.
_GENERATION_SPAN = "claude_code.generation"


def _usage_claude_stack(llm_stub: LlmStub):
    def build(res, variants):
        config = build_claude_agent_stack(res, variants)
        config.env["SANDBOX_FAKE_RUNNER"] = _USAGE_RUNNER
        return config

    return build


async def _drive_attributed(stack: TaiStack, tags: list[str], metadata: dict) -> list[dict]:
    """Deposit ``RunAttribution`` then drive one scripted ``claude_code`` turn through the run
    chokepoint; return the span records this call appended."""
    before = len(stack.records("monitor_spans"))
    async with stack.mcp() as mcp:
        await mcp.call_tool(
            "e2e_sandbox_attribution_probe",
            {
                "tags": tags,
                "metadata": metadata,
                "inner_tool": "claude_code",
                "inner_arguments": {"user_message": "hi"},
            },
        )
    return [json.loads(rec) for rec in stack.records("monitor_spans")[before:]]


async def _drive_unattributed(stack: TaiStack) -> list[dict]:
    """Drive one scripted ``claude_code`` turn with NO ambient attribution; return the span
    records this call appended."""
    before = len(stack.records("monitor_spans"))
    async with stack.mcp() as mcp:
        await mcp.call_tool("claude_code", {"user_message": "hi"})
    return [json.loads(rec) for rec in stack.records("monitor_spans")[before:]]


async def test_attribution_stamps_the_runs_own_span(fresh_stack, llm_stub: LlmStub, uniq: Callable[[str], str]) -> None:
    stack: TaiStack = fresh_stack(_usage_claude_stack(llm_stub), resource_kwargs={"llm_base_url": llm_stub.base_url})

    # A run under a deposited attribution: its OWN generation span is recorded STAMPED with the
    # attribution's tags/metadata — proving the span opened inside the scope.
    tag = uniq("attr")
    metadata = {"origin": uniq("flow")}
    spans = [s for s in await _drive_attributed(stack, [tag], metadata) if s["name"] == _GENERATION_SPAN]
    assert len(spans) == 1, spans
    attribution = spans[0]["attribution"]
    assert attribution is not None, spans[0]
    assert tag in attribution["tags"], attribution
    assert attribution["metadata"] == metadata, attribution

    # A run with NO ambient attribution: its generation span carries no attribution — no
    # spurious stamp.
    plain = [s for s in await _drive_unattributed(stack) if s["name"] == _GENERATION_SPAN]
    assert len(plain) == 1, plain
    assert plain[0]["attribution"] is None, plain[0]


async def test_cost_is_groupable_by_attribution(fresh_stack, llm_stub: LlmStub, uniq: Callable[[str], str]) -> None:
    stack: TaiStack = fresh_stack(_usage_claude_stack(llm_stub), resource_kwargs={"llm_base_url": llm_stub.base_url})

    # Two runs under two DISTINCT attribution tag-sets land two distinguishable
    # attribution-stamped spans — the "cost groupable by attribution" property, read at the
    # recorder without any billing module.
    tag_a = uniq("group-a")
    tag_b = uniq("group-b")
    spans_a = [s for s in await _drive_attributed(stack, [tag_a], {}) if s["name"] == _GENERATION_SPAN]
    spans_b = [s for s in await _drive_attributed(stack, [tag_b], {}) if s["name"] == _GENERATION_SPAN]

    assert len(spans_a) == 1, spans_a
    assert len(spans_b) == 1, spans_b
    assert spans_a[0]["attribution"] is not None, spans_a[0]
    assert spans_b[0]["attribution"] is not None, spans_b[0]
    assert spans_a[0]["attribution"]["tags"] == [tag_a], spans_a[0]
    assert spans_b[0]["attribution"]["tags"] == [tag_b], spans_b[0]
    assert spans_a[0]["attribution"]["tags"] != spans_b[0]["attribution"]["tags"]
