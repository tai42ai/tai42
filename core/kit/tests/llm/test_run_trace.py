"""Unit tests for the per-invoke run-trace seam (``tai42_kit.llm.run_trace``).

``resolve_trace_context`` resolves the ONE trace lineage for a run — an explicitly
propagated ``MONITORING_TRACE_ID_KEY`` wins, else the ambient deposit a driver left, else
a freshly minted 32-hex root. ``bind_run_trace`` resolves that lineage and returns the
per-invoke ``RunnableConfig`` with the kit's monitoring callback handler appended after
the caller's when the active writer records, and nothing appended when it does not. Both
are exercised against a fake app bound to ``tai42_app``.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as otel_context
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import (
    MONITORING_PARENT_SPAN_ID_KEY,
    MONITORING_TRACE_ID_KEY,
    TraceContext,
    ambient_trace_context,
)

from tai42_kit.llm.monitoring_callbacks import MonitoringCallbackHandler
from tai42_kit.llm.run_trace import RunTrace, bind_run_trace, resolve_trace_context


class _RecordingWriter:
    def __init__(self) -> None:
        self.recording = True

    def is_recording(self) -> bool:
        return self.recording


class _Monitoring:
    def __init__(self, writer: _RecordingWriter) -> None:
        self.writer = writer


class _MonitoringFacet:
    def __init__(self, writer: _RecordingWriter) -> None:
        self._backend = _Monitoring(writer)

    @property
    def active(self) -> _Monitoring:
        return self._backend


class _FakeApp:
    def __init__(self, writer: _RecordingWriter) -> None:
        self.monitoring = _MonitoringFacet(writer)


@pytest.fixture
def writer() -> Any:
    recording = _RecordingWriter()
    with tai42_app.bound(_FakeApp(recording)):
        yield recording


def test_resolve_explicit_trace_id_wins(writer: _RecordingWriter) -> None:
    with ambient_trace_context(TraceContext(trace_id="ambient", parent_span_id="anchor")):
        ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: "explicit"}})
    assert ctx.trace_id == "explicit"
    assert ctx.parent_span_id is None


def test_resolve_explicit_parent_anchor(writer: _RecordingWriter) -> None:
    ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: "t", MONITORING_PARENT_SPAN_ID_KEY: "p"}})
    assert (ctx.trace_id, ctx.parent_span_id) == ("t", "p")


def test_resolve_adopts_ambient_when_no_explicit(writer: _RecordingWriter) -> None:
    with ambient_trace_context(TraceContext(trace_id="ambient", parent_span_id="anchor")):
        ctx = resolve_trace_context()
    assert ctx.trace_id == "ambient"
    assert ctx.parent_span_id == "anchor"


def test_resolve_mints_fresh_32_hex_root(writer: _RecordingWriter) -> None:
    ctx = resolve_trace_context()
    assert ctx.trace_id is not None
    assert len(ctx.trace_id) == 32
    assert "-" not in ctx.trace_id
    assert ctx.parent_span_id is None


def _handlers(trace: RunTrace) -> list[MonitoringCallbackHandler]:
    return [cb for cb in trace.config["callbacks"] if isinstance(cb, MonitoringCallbackHandler)]


def test_bind_returns_run_trace_with_the_handler_bound_to_the_context(writer: _RecordingWriter) -> None:
    trace = bind_run_trace({"configurable": {MONITORING_TRACE_ID_KEY: "t1"}})
    assert isinstance(trace, RunTrace)
    assert trace.context.trace_id == "t1"
    (handler,) = _handlers(trace)
    assert handler.trace_context == trace.context
    assert handler.writer is writer
    assert handler.grouping_nodes == frozenset()
    assert handler.chain_payloads == "references"


def test_bind_passes_the_recording_options(writer: _RecordingWriter) -> None:
    trace = bind_run_trace(grouping_nodes=frozenset({"model"}), chain_payloads="producer")
    (handler,) = _handlers(trace)
    assert handler.grouping_nodes == frozenset({"model"})
    assert handler.chain_payloads == "producer"


def test_bind_binds_no_handler_when_the_writer_records_nothing(writer: _RecordingWriter) -> None:
    writer.recording = False
    existing = BaseCallbackHandler()
    trace = bind_run_trace({"callbacks": [existing]})
    assert trace.config["callbacks"] == [existing]


def test_bind_appends_callbacks_after_the_callers(writer: _RecordingWriter) -> None:
    existing = BaseCallbackHandler()
    trace = bind_run_trace({"callbacks": [existing], "configurable": {MONITORING_TRACE_ID_KEY: "t1"}})
    assert trace.config["callbacks"][0] is existing
    assert len(trace.config["callbacks"]) == 2


def test_bind_does_not_mutate_the_input(writer: _RecordingWriter) -> None:
    existing = {"configurable": {MONITORING_TRACE_ID_KEY: "t1"}, "callbacks": ["cb"]}
    bind_run_trace(existing)
    assert existing == {"configurable": {MONITORING_TRACE_ID_KEY: "t1"}, "callbacks": ["cb"]}


def test_bind_yields_independent_copies(writer: _RecordingWriter) -> None:
    shared = {"configurable": {}, "callbacks": ["cb"]}
    first = bind_run_trace(shared)
    second = bind_run_trace(shared)
    assert first.config is not second.config
    assert first.config["configurable"] is not second.config["configurable"]
    assert first.config["callbacks"] is not second.config["callbacks"]
    assert _handlers(first)[0] is not _handlers(second)[0]
    assert shared == {"configurable": {}, "callbacks": ["cb"]}


def test_bind_has_no_otel_side_effect(writer: _RecordingWriter) -> None:
    key = otel_context.create_key("run-trace-probe")
    token = otel_context.attach(otel_context.set_value(key, "caller-attribution"))
    try:
        bind_run_trace()
        # A root bind must not attach a fresh OTel context — the caller's attribution
        # (the value a backend stamped on the ambient context) must survive the build.
        assert otel_context.get_value(key) == "caller-attribution"
    finally:
        otel_context.detach(token)
