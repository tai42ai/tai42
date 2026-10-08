"""The record-reference probe: a synthetic two-node graph whose second record references the first.

``e2e_record_probe`` runs a LangGraph graph bound with ``bind_run_trace(chain_payloads="producer")``:
node ``first`` records its own input and output (a large value) through ``update_current_span``,
node ``second`` records as its input a ``payload_ref`` to ``first``'s output instead of a copy.
It returns the trace and the two record ids so a spec reads the trace back and resolves the
reference through the platform's resolved-value door.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from tai42_contract.app import tai42_app
from tai42_kit.llm import bind_run_trace
from tai42_kit.monitoring import payload_ref

FIRST_VALUE = "alpha-" * 2000


class _ProbeState(TypedDict, total=False):
    first_span: str
    second_span: str


def _first(state: _ProbeState, config: RunnableConfig) -> dict[str, Any]:
    writer = tai42_app.monitoring.active.writer
    span_id = writer.current_span_id()
    if span_id is None:
        raise RuntimeError("the first node ran with no current record")
    writer.update_current_span(input_={"step": "first"}, output={"value": FIRST_VALUE})
    return {"first_span": span_id}


def _second(state: _ProbeState, config: RunnableConfig) -> dict[str, Any]:
    writer = tai42_app.monitoring.active.writer
    span_id = writer.current_span_id()
    if span_id is None:
        raise RuntimeError("the second node ran with no current record")
    first_span = state.get("first_span")
    if first_span is None:
        raise RuntimeError("the second node ran before the first recorded its span")
    writer.update_current_span(
        input_={"from_first": payload_ref(first_span, "output", "/value")}, output={"done": True}
    )
    return {"second_span": span_id}


def _graph() -> Any:
    builder = StateGraph(_ProbeState)
    builder.add_node("first", _first)
    builder.add_node("second", _second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    return builder.compile()


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_record_probe() -> dict[str, str]:
    """Run the two-node reference graph; return ``{"trace_id", "first_span", "second_span"}``."""
    trace = bind_run_trace(chain_payloads="producer")
    final = await _graph().ainvoke({}, trace.config)
    return {
        "trace_id": str(trace.context.trace_id),
        "first_span": final["first_span"],
        "second_span": final["second_span"],
    }
