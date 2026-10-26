"""The run-trace opener: ``resolve_trace_context``, ``begin_run_trace``, ``open_run_trace`` and ``open_lineage_span``.

Driven through the real :class:`OtelWriter` bound to ``tai42_app`` (an in-memory exporter), and a writer that
records nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.monitoring import (
    MONITORING_PARENT_SPAN_ID_KEY,
    MONITORING_TRACE_ID_KEY,
    SpanKind,
    TraceContext,
    ambient_trace_context,
    get_ambient_trace_context,
)

from tai42_kit.monitoring import (
    InvalidRunTraceError,
    begin_run_trace,
    open_lineage_span,
    open_run_trace,
    resolve_trace_context,
)
from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.otel import attributes as attr

from .conftest import attrs, decoded, finished

AMBIENT = "a" * 32
ANCHOR = "b" * 16
PARENT = "c" * 16
T1 = "1" * 32


class _Monitoring:
    def __init__(self, writer: Any) -> None:
        self.writer = writer


class _MonitoringFacet:
    def __init__(self, writer: Any) -> None:
        self._backend = _Monitoring(writer)

    @property
    def active(self) -> _Monitoring:
        return self._backend


class _FakeApp:
    def __init__(self, writer: Any) -> None:
        self.monitoring = _MonitoringFacet(writer)


class _SilentWriter:
    """A writer that records nothing; opening a span through it fails the test."""

    def is_recording(self) -> bool:
        return False

    def open_span(self, **_: Any) -> Any:
        raise AssertionError("a writer that does not record is never asked to open a span")


@pytest.fixture
def bound(writer: OtelWriter) -> Iterator[OtelWriter]:
    with tai42_app.bound(_FakeApp(writer)):
        yield writer


@pytest.fixture
def silent() -> Iterator[_SilentWriter]:
    w = _SilentWriter()
    with tai42_app.bound(_FakeApp(w)):
        yield w


def _hex(span: ReadableSpan) -> tuple[str, str]:
    assert span.context is not None
    return format(span.context.trace_id, "032x"), format(span.context.span_id, "016x")


def _parent(span: ReadableSpan) -> str | None:
    return format(span.parent.span_id, "016x") if span.parent is not None else None


def _by_name(writer: OtelWriter, exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {s.name: s for s in finished(writer, exporter)}


# --- resolve_trace_context ---------------------------------------------------------------


def test_resolve_refuses_an_explicit_trace_without_a_parent(silent: _SilentWriter) -> None:
    with (
        ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)),
        pytest.raises(InvalidRunTraceError) as raised,
    ):
        resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: T1}})
    assert str(raised.value) == (
        f"configurable[{MONITORING_TRACE_ID_KEY!r}] names a trace without a parent span: set "
        f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] to the span the run nests under"
    )
    assert error_kind(raised.value) is ErrorKind.BAD_INPUT


def test_resolve_explicit_pair(silent: _SilentWriter) -> None:
    ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: T1, MONITORING_PARENT_SPAN_ID_KEY: PARENT}})
    assert ctx == TraceContext(trace_id=T1, parent_span_id=PARENT)


def test_resolve_adopts_ambient_when_no_explicit(silent: _SilentWriter) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context()
    assert ctx == TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)


def test_resolve_mints_nothing(silent: _SilentWriter) -> None:
    assert resolve_trace_context() is None
    assert resolve_trace_context({"configurable": {}}) is None


@pytest.mark.parametrize(
    "trace_id", [str(uuid.uuid4()), uuid.uuid4().hex.upper(), "0" * 32, "e" * 31, "not-a-trace-id"]
)
def test_resolve_refuses_an_explicit_trace_id_no_writer_can_record(silent: _SilentWriter, trace_id: str) -> None:
    with pytest.raises(InvalidRunTraceError) as raised:
        resolve_trace_context(
            {"configurable": {MONITORING_TRACE_ID_KEY: trace_id, MONITORING_PARENT_SPAN_ID_KEY: PARENT}}
        )
    assert str(raised.value) == (
        f"configurable[{MONITORING_TRACE_ID_KEY!r}] must be a 32-character lowercase hex trace id that is not all "
        f"zeros; got {trace_id!r}"
    )
    assert error_kind(raised.value) is ErrorKind.BAD_INPUT


@pytest.mark.parametrize("anchor", ["short", "0" * 16, "B" * 16, "b" * 32])
def test_resolve_refuses_a_parent_that_is_not_a_span_id(silent: _SilentWriter, anchor: str) -> None:
    with pytest.raises(InvalidRunTraceError) as raised:
        resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: T1, MONITORING_PARENT_SPAN_ID_KEY: anchor}})
    assert str(raised.value) == (
        f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] must be a 16-character lowercase hex span id that is not "
        f"all zeros; got {anchor!r}"
    )
    assert error_kind(raised.value) is ErrorKind.BAD_INPUT


def test_resolve_refuses_a_malformed_parent_under_the_ambient_trace(silent: _SilentWriter) -> None:
    with (
        ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)),
        pytest.raises(InvalidRunTraceError, match=MONITORING_PARENT_SPAN_ID_KEY),
    ):
        resolve_trace_context({"configurable": {MONITORING_PARENT_SPAN_ID_KEY: "short"}})


def test_resolve_pairs_an_explicit_parent_with_the_ambient_trace(silent: _SilentWriter) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context({"configurable": {MONITORING_PARENT_SPAN_ID_KEY: PARENT}})
    assert ctx == TraceContext(trace_id=AMBIENT, parent_span_id=PARENT)


def test_resolve_refuses_an_explicit_parent_with_no_ambient_trace(silent: _SilentWriter) -> None:
    with pytest.raises(InvalidRunTraceError) as raised:
        resolve_trace_context({"configurable": {MONITORING_PARENT_SPAN_ID_KEY: PARENT}})
    assert str(raised.value) == (
        f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] names a parent span with no trace: set "
        f"configurable[{MONITORING_TRACE_ID_KEY!r}] beside it"
    )


@pytest.mark.parametrize("absent", ["", None])
def test_resolve_reads_an_empty_explicit_trace_id_as_absent(silent: _SilentWriter, absent: str | None) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        ctx = resolve_trace_context({"configurable": {MONITORING_TRACE_ID_KEY: absent}})
    assert ctx == TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)


# --- begin_run_trace -------------------------------------------------------------------------


def test_begin_refuses_a_config_and_opens_nothing(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    with pytest.raises(InvalidRunTraceError, match=MONITORING_TRACE_ID_KEY):
        begin_run_trace({"configurable": {MONITORING_TRACE_ID_KEY: str(uuid.uuid4())}})
    assert finished(bound, exporter) == []


def test_begin_with_nothing_live_opens_one_run_root(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    root = begin_run_trace()
    assert get_ambient_trace_context() is None
    assert bound.current_span_id() is None
    root.end()
    (span,) = finished(bound, exporter)
    assert span.name == "run"
    assert attrs(span)[attr.SPAN_KIND] == SpanKind.CHAIN.value
    assert span.parent is None
    trace_id, span_id = _hex(span)
    assert root.context == TraceContext(trace_id=trace_id, parent_span_id=span_id)


def test_step_makes_the_span_current_and_deposits_the_lineage(bound: OtelWriter) -> None:
    root = begin_run_trace()
    assert root.context is not None
    with root.step():
        assert bound.current_span_id() == root.context.parent_span_id
        assert bound.current_trace_id() == root.context.trace_id
        assert get_ambient_trace_context() == root.context
    assert bound.current_span_id() is None
    assert get_ambient_trace_context() is None
    root.end()


def test_fail_records_the_error_on_the_span(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    root = begin_run_trace()
    root.fail(ValueError("x"))
    root.end()
    (span,) = finished(bound, exporter)
    assert attrs(span)[attr.LEVEL] == "ERROR"
    assert attrs(span)[attr.STATUS_MESSAGE] == "x"


def test_end_ends_the_span_once(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    root = begin_run_trace()
    root.end()
    root.end()
    assert len(finished(bound, exporter)) == 1
    assert bound.export_health().records_failed == 0


async def test_end_from_another_task_raises_nothing(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    root = begin_run_trace()
    await asyncio.create_task(asyncio.to_thread(root.end))
    await asyncio.create_task(_end(root))
    assert len(finished(bound, exporter)) == 1


async def _end(root: Any) -> None:
    root.end()


def test_begin_keeps_a_lineage_in_the_config_current_and_deposited_in_each_step(
    bound: OtelWriter, exporter: InMemorySpanExporter
) -> None:
    lineage = TraceContext(trace_id=T1, parent_span_id=PARENT)
    root = begin_run_trace({"configurable": {MONITORING_TRACE_ID_KEY: T1, MONITORING_PARENT_SPAN_ID_KEY: PARENT}})
    assert root.context == lineage
    assert bound.current_span_id() is None
    with root.step():
        assert (bound.current_trace_id(), bound.current_span_id()) == (T1, PARENT)
        assert get_ambient_trace_context() == lineage
        with open_run_trace() as nested:
            assert nested.context == lineage
        bound.open_span(name="inside", kind=SpanKind.CHAIN).end()
    assert bound.current_span_id() is None
    assert get_ambient_trace_context() is None
    root.end()
    spans = _by_name(bound, exporter)
    assert list(spans) == ["inside"]
    assert _hex(spans["inside"])[0] == T1
    assert _parent(spans["inside"]) == PARENT


def test_a_lineage_in_the_config_that_is_the_live_span_keeps_that_span_current(
    bound: OtelWriter, exporter: InMemorySpanExporter
) -> None:
    with open_run_trace() as outer:
        assert outer.context is not None
        config = {
            "configurable": {
                MONITORING_TRACE_ID_KEY: outer.context.trace_id,
                MONITORING_PARENT_SPAN_ID_KEY: outer.context.parent_span_id,
            }
        }
        root = begin_run_trace(config)
        with root.step():
            bound.update_current_span(metadata={"reached": "the run span"})
        root.end()
    assert decoded(_by_name(bound, exporter)["run"], attr.METADATA) == {"reached": "the run span"}


@pytest.mark.parametrize("live", [False, True])
def test_begin_keeps_the_ambient_deposit(bound: OtelWriter, exporter: InMemorySpanExporter, live: bool) -> None:
    deposit = TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)
    outer = bound.open_span(name="outer", kind=SpanKind.CHAIN, activate=live)
    with ambient_trace_context(deposit):
        root = begin_run_trace()
        before = bound.current_span_id()
        with root.step():
            assert bound.current_span_id() == before
            assert get_ambient_trace_context() == deposit
        root.end()
    assert root.context == deposit
    outer.end()
    assert [s.name for s in finished(bound, exporter)] == ["outer"]


def test_begin_under_a_live_span_opens_a_child_run(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    with bound.start_span(name="outer", kind=SpanKind.CHAIN) as outer:
        live_trace = bound.current_trace_id()
        root = begin_run_trace()
        assert bound.current_span_id() == outer.id
        root.end()
    spans = _by_name(bound, exporter)
    run_trace, run_id = _hex(spans["run"])
    assert _parent(spans["run"]) == outer.id
    assert run_trace == live_trace
    assert root.context == TraceContext(trace_id=live_trace, parent_span_id=run_id)


def test_begin_with_a_writer_that_records_nothing(silent: _SilentWriter) -> None:
    root = begin_run_trace()
    assert root.context is None
    with root.step():
        assert get_ambient_trace_context() is None
    root.fail(ValueError("x"))
    root.end()


# --- open_run_trace ------------------------------------------------------------------------


def test_open_run_trace_holds_the_lineage_for_the_block(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    with open_run_trace() as root:
        context = root.context
        assert context is not None
        assert get_ambient_trace_context() == context
        assert bound.current_span_id() == context.parent_span_id
        bound.open_span(name="first", kind=SpanKind.CHAIN).end()
        bound.open_span(name="second", kind=SpanKind.CHAIN).end()
        assert "run" not in _by_name(bound, exporter)
    assert get_ambient_trace_context() is None
    assert bound.current_span_id() is None
    spans = _by_name(bound, exporter)
    run_id = _hex(spans["run"])[1]
    assert _parent(spans["first"]) == run_id
    assert _parent(spans["second"]) == run_id


def test_open_run_trace_records_a_raise_and_propagates(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="boom"), open_run_trace():
        raise ValueError("boom")
    (span,) = finished(bound, exporter)
    assert attrs(span)[attr.LEVEL] == "ERROR"
    assert attrs(span)[attr.STATUS_MESSAGE] == "boom"


async def test_open_run_trace_records_no_level_for_a_cancellation(
    bound: OtelWriter, exporter: InMemorySpanExporter
) -> None:
    async def run() -> None:
        with open_run_trace():
            await asyncio.sleep(10)

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (span,) = finished(bound, exporter)
    assert attr.LEVEL not in attrs(span)


def test_open_run_trace_with_a_writer_that_records_nothing(silent: _SilentWriter) -> None:
    with open_run_trace(input_={"q": "hi"}) as root:
        assert root.context is None
        assert get_ambient_trace_context() is None
        root.record_output("answer")


# --- open_lineage_span ---------------------------------------------------------------------


def test_open_lineage_span_continues_the_handed_over_lineage(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    parent = TraceContext(trace_id=T1, parent_span_id=PARENT)
    with open_lineage_span("n", parent=parent) as root:
        context = root.context
        assert context is not None
        assert context.trace_id == T1
        assert get_ambient_trace_context() == context
        assert bound.current_span_id() == context.parent_span_id
        assert finished(bound, exporter) == []
    (span,) = finished(bound, exporter)
    assert span.name == "n"
    assert _hex(span) == (T1, context.parent_span_id)
    assert _parent(span) == PARENT
    assert get_ambient_trace_context() is None


def test_open_lineage_span_records_a_raise_and_propagates(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    parent = TraceContext(trace_id=T1, parent_span_id=PARENT)
    with pytest.raises(RuntimeError, match="bad"), open_lineage_span("n", parent=parent):
        raise RuntimeError("bad")
    (span,) = finished(bound, exporter)
    assert attrs(span)[attr.LEVEL] == "ERROR"


def test_open_lineage_span_with_a_writer_that_records_nothing(silent: _SilentWriter) -> None:
    with open_lineage_span("n", parent=TraceContext(trace_id=T1, parent_span_id=PARENT), input_={"a": 1}) as root:
        assert root.context is None
        root.record_output({"b": 2})
        assert get_ambient_trace_context() is None


# --- the run's input and output ------------------------------------------------------------

RUN_INPUT = {"messages": ["hello"], "count": 2}
RUN_OUTPUT = {"answer": "world"}
IN = attr.input_attribute(SpanKind.CHAIN)
OUT = attr.output_attribute(SpanKind.CHAIN)


def test_open_run_trace_records_the_run_input_and_output_on_the_root(
    bound: OtelWriter, exporter: InMemorySpanExporter
) -> None:
    with open_run_trace(input_=RUN_INPUT) as root:
        root.record_output(RUN_OUTPUT)
    (span,) = finished(bound, exporter)
    assert span.name == "run"
    assert attrs(span)[IN] == bound.encode_field(RUN_INPUT, "run")
    assert attrs(span)[OUT] == bound.encode_field(RUN_OUTPUT, "run")


def test_begin_run_trace_records_the_run_input_and_output_on_the_root(
    bound: OtelWriter, exporter: InMemorySpanExporter
) -> None:
    root = begin_run_trace(input_=RUN_INPUT)
    with root.step():
        pass
    root.record_output(RUN_OUTPUT)
    root.end()
    (span,) = finished(bound, exporter)
    assert attrs(span)[IN] == bound.encode_field(RUN_INPUT, "run")
    assert attrs(span)[OUT] == bound.encode_field(RUN_OUTPUT, "run")


def test_a_failed_run_keeps_its_input_and_records_the_error_without_an_output(
    bound: OtelWriter, exporter: InMemorySpanExporter
) -> None:
    with pytest.raises(ValueError, match="boom"), open_run_trace(input_=RUN_INPUT):
        raise ValueError("boom")
    (span,) = finished(bound, exporter)
    assert attrs(span)[IN] == bound.encode_field(RUN_INPUT, "run")
    assert OUT not in attrs(span)
    assert attrs(span)[attr.LEVEL] == "ERROR"
    assert attrs(span)[attr.STATUS_MESSAGE] == "boom"


def test_a_kept_lineage_records_no_input_or_output(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    with ambient_trace_context(TraceContext(trace_id=AMBIENT, parent_span_id=ANCHOR)):
        with open_run_trace(input_=RUN_INPUT) as root:
            root.record_output(RUN_OUTPUT)
        later = begin_run_trace(input_=RUN_INPUT)
        later.record_output(RUN_OUTPUT)
        later.end()
    assert finished(bound, exporter) == []


def test_open_lineage_span_records_the_call_input_and_output(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    parent = TraceContext(trace_id=T1, parent_span_id=PARENT)
    with open_lineage_span("n", parent=parent, input_=RUN_INPUT) as root:
        root.record_output(RUN_OUTPUT)
    (span,) = finished(bound, exporter)
    assert attrs(span)[IN] == bound.encode_field(RUN_INPUT, "n")
    assert attrs(span)[OUT] == bound.encode_field(RUN_OUTPUT, "n")


def test_an_ended_root_records_no_output(bound: OtelWriter, exporter: InMemorySpanExporter) -> None:
    root = begin_run_trace(input_=RUN_INPUT)
    root.end()
    root.record_output(RUN_OUTPUT)
    (span,) = finished(bound, exporter)
    assert OUT not in attrs(span)
