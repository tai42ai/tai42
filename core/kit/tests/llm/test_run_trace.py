"""Unit tests for the per-invoke run-trace seam (``tai42_kit.llm.run_trace``).

``resolve_trace_context`` resolves the ONE trace lineage for a run — an explicitly
propagated ``MONITORING_TRACE_ID_KEY`` wins, else the ambient deposit a driver left, else
a freshly minted 32-hex root. ``bind_run_trace`` resolves that lineage and returns the
per-invoke ``RunnableConfig`` with the kit's monitoring callback handler appended after
the caller's when the active writer records, and nothing appended when it does not. Both
are exercised against a fake app bound to ``tai42_app``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as otel_context
from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.monitoring import (
    MONITORING_PARENT_SPAN_ID_KEY,
    MONITORING_TRACE_ID_KEY,
    TraceContext,
    ambient_trace_context,
)

from tai42_kit.llm.monitoring_callbacks import MonitoringCallbackHandler
from tai42_kit.llm.run_trace import InvalidRunTraceError, RunTrace, bind_run_trace, resolve_trace_context

EXPLICIT = "e" * 32
AMBIENT = "a" * 32
ANCHOR = "b" * 16
PARENT = "c" * 16
T1 = "1" * 32


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
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: EXPLICIT}})
    assert ctx.trace_id == EXPLICIT
    assert ctx.parent_span_id is None


def test_resolve_explicit_parent_anchor(writer: _RecordingWriter) -> None:
    ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: T1, MONITORING_PARENT_SPAN_ID_KEY: PARENT}})
    assert (ctx.trace_id, ctx.parent_span_id) == (T1, PARENT)


def test_resolve_adopts_ambient_when_no_explicit(writer: _RecordingWriter) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context()
    assert ctx.trace_id == AMBIENT
    assert ctx.parent_span_id == ANCHOR


def test_resolve_mints_fresh_32_hex_root(writer: _RecordingWriter) -> None:
    ctx = resolve_trace_context()
    assert ctx.trace_id is not None
    assert len(ctx.trace_id) == 32
    assert "-" not in ctx.trace_id
    assert ctx.parent_span_id is None


@pytest.mark.parametrize(
    "trace_id", [str(uuid.uuid4()), uuid.uuid4().hex.upper(), "0" * 32, "e" * 31, "not-a-trace-id"]
)
def test_resolve_refuses_an_explicit_trace_id_no_writer_can_record(writer: _RecordingWriter, trace_id: str) -> None:
    with pytest.raises(InvalidRunTraceError) as raised:
        resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: trace_id}})
    assert str(raised.value) == (
        f"configurable[{MONITORING_TRACE_ID_KEY!r}] must be a 32-character lowercase hex trace id that is not all "
        f"zeros; got {trace_id!r}"
    )
    assert error_kind(raised.value) is ErrorKind.BAD_INPUT


@pytest.mark.parametrize("anchor", ["short", "0" * 16, "B" * 16, "b" * 32])
def test_resolve_refuses_a_parent_anchor_that_is_not_a_span_id(writer: _RecordingWriter, anchor: str) -> None:
    with pytest.raises(InvalidRunTraceError) as raised:
        resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: T1, MONITORING_PARENT_SPAN_ID_KEY: anchor}})
    assert str(raised.value) == (
        f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] must be a 16-character lowercase hex span id that is not "
        f"all zeros; got {anchor!r}"
    )
    assert error_kind(raised.value) is ErrorKind.BAD_INPUT


def test_resolve_refuses_a_callers_anchor_under_the_ambient_trace(writer: _RecordingWriter) -> None:
    with (
        ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)),
        pytest.raises(InvalidRunTraceError, match=MONITORING_PARENT_SPAN_ID_KEY),
    ):
        resolve_trace_context({"configurable": {MONITORING_PARENT_SPAN_ID_KEY: "short"}})


def test_resolve_takes_a_callers_anchor_under_the_ambient_trace(writer: _RecordingWriter) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context({"configurable": {MONITORING_PARENT_SPAN_ID_KEY: PARENT}})
    assert (ctx.trace_id, ctx.parent_span_id) == (AMBIENT, PARENT)


@pytest.mark.parametrize("absent", ["", None])
def test_resolve_reads_an_empty_explicit_trace_id_as_absent(writer: _RecordingWriter, absent: str | None) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: absent}})
    assert (ctx.trace_id, ctx.parent_span_id) == (AMBIENT, ANCHOR)


def test_bind_propagates_the_refusal(writer: _RecordingWriter) -> None:
    with pytest.raises(InvalidRunTraceError, match=MONITORING_TRACE_ID_KEY):
        bind_run_trace({"configurable": {MONITORING_TRACE_ID_KEY: str(uuid.uuid4())}})


def _handlers(trace: RunTrace) -> list[MonitoringCallbackHandler]:
    return [cb for cb in trace.config["callbacks"] if isinstance(cb, MonitoringCallbackHandler)]


def test_bind_returns_run_trace_with_the_handler_bound_to_the_context(writer: _RecordingWriter) -> None:
    trace = bind_run_trace({"configurable": {MONITORING_TRACE_ID_KEY: T1}})
    assert isinstance(trace, RunTrace)
    assert trace.context.trace_id == T1
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
    trace = bind_run_trace({"callbacks": [existing], "configurable": {MONITORING_TRACE_ID_KEY: T1}})
    assert trace.config["callbacks"][0] is existing
    assert len(trace.config["callbacks"]) == 2


def test_bind_does_not_mutate_the_input(writer: _RecordingWriter) -> None:
    existing = {"configurable": {MONITORING_TRACE_ID_KEY: T1}, "callbacks": ["cb"]}
    bind_run_trace(existing)
    assert existing == {"configurable": {MONITORING_TRACE_ID_KEY: T1}, "callbacks": ["cb"]}


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
