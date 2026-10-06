"""Unit tests for the per-invoke run-trace seam (``tai42_kit.llm.run_trace``).

``resolve_trace_context`` resolves the ONE trace lineage for a run — an explicitly
propagated ``monitoring_trace_id`` wins, else the ambient deposit a driver left, else
a freshly minted 32-hex root. ``bind_run_trace`` resolves that lineage, asks the active
monitoring backend for the run's callbacks, and returns the per-invoke ``RunnableConfig``
carrying them appended after the caller's — a PURE build with no OpenTelemetry side
effect. Both are exercised against a fake app bound to ``tai42_app`` whose writer records
each :class:`TraceContext` it is handed and returns real callback handlers.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as otel_context
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import TraceContext, ambient_trace_context

from tai42_kit.llm.run_trace import RunTrace, bind_run_trace, resolve_trace_context


class _RecordingHandler(BaseCallbackHandler):
    def __init__(self, trace_id: str | None) -> None:
        self.trace_id = trace_id


class _RecordingWriter:
    def __init__(self) -> None:
        self.contexts: list[TraceContext] = []
        self.raise_on_callbacks = False

    def get_monitoring_callbacks(self, ctx: TraceContext) -> list[object]:
        if self.raise_on_callbacks:
            raise RuntimeError("writer boom")
        self.contexts.append(ctx)
        return [_RecordingHandler(ctx.trace_id)]


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
        ctx = resolve_trace_context({"configurable": {"monitoring_trace_id": "explicit"}})
    assert ctx.trace_id == "explicit"
    assert ctx.parent_span_id is None


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


def test_bind_returns_run_trace_with_context_and_callbacks(writer: _RecordingWriter) -> None:
    trace = bind_run_trace({"configurable": {"monitoring_trace_id": "t1"}})
    assert isinstance(trace, RunTrace)
    assert trace.context.trace_id == "t1"
    # The one TraceContext the writer was handed matches the bound context.
    assert writer.contexts[-1].trace_id == "t1"
    # The config carries exactly the backend's callbacks for this run.
    assert [getattr(cb, "trace_id", None) for cb in trace.config["callbacks"]] == ["t1"]


def test_bind_appends_callbacks_after_the_callers(writer: _RecordingWriter) -> None:
    existing = BaseCallbackHandler()
    trace = bind_run_trace({"callbacks": [existing], "configurable": {"monitoring_trace_id": "t1"}})
    assert trace.config["callbacks"][0] is existing
    assert len(trace.config["callbacks"]) == 2


def test_bind_does_not_mutate_the_input(writer: _RecordingWriter) -> None:
    existing = {"configurable": {"monitoring_trace_id": "t1"}, "callbacks": ["cb"]}
    bind_run_trace(existing)
    assert existing == {"configurable": {"monitoring_trace_id": "t1"}, "callbacks": ["cb"]}


def test_bind_yields_independent_copies(writer: _RecordingWriter) -> None:
    shared = {"configurable": {}, "callbacks": ["cb"]}
    first = bind_run_trace(shared)
    second = bind_run_trace(shared)
    assert first.config is not second.config
    assert first.config["configurable"] is not second.config["configurable"]
    assert first.config["callbacks"] is not second.config["callbacks"]
    assert shared == {"configurable": {}, "callbacks": ["cb"]}


def test_bind_propagates_a_writer_error(writer: _RecordingWriter) -> None:
    writer.raise_on_callbacks = True
    with pytest.raises(RuntimeError, match="writer boom"):
        bind_run_trace()


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
