"""The ``monitor`` builtin tool extension (WRAPPER) through the stack.

Observable effect: a standalone call of the branch tool is traced as one live
``SpanKind.TOOL`` span. The fixture monitoring backend records every span it opens
onto the probe channel (``e2e:rec:monitor_spans``), so calling ``e2e_echo_monitor``
must leave a span record naming the wrapped tool and carrying its call input."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def test_monitor_emits_a_tool_span_for_a_standalone_call(
    extensions_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    payload = f"monitored-{uniq('body')}"

    async with extensions_stack.mcp() as mcp:
        result = await mcp.call_tool("e2e_echo_monitor", {"payload": payload})
    assert payload in str(result.data)

    def _matching() -> list[dict]:
        spans = [json.loads(rec) for rec in extensions_stack.records("monitor_spans")]
        return [s for s in spans if payload in s.get("input", "")]

    async def emitted() -> bool:
        return len(_matching()) >= 1

    await wait_for_async(emitted, deadline=5.0, message="monitor extension never emitted a span for the call")
    matching = _matching()
    assert len(matching) == 1, f"monitor emitted the wrong number of spans: {matching}"
    span = matching[0]
    assert span["name"] == "e2e_echo", f"monitor span did not name the wrapped tool: {span}"
    assert span["kind"] == "TOOL", f"monitor span was not a TOOL span: {span}"
