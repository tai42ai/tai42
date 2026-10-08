"""``InMemoryOtelReader`` is the inverse of the writer's attribute schema."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.monitoring import (
    STEP_ROLE_METADATA_KEY,
    TIMING_METADATA_KEY,
    Measure,
    MetricsQuery,
    MonitoringLevel,
    MonitoringReadNotSupportedError,
    ObservationNotFoundError,
    SpanKind,
    StepRole,
    TokenUsage,
    TraceContext,
    TraceNotFoundError,
)

from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.testing import InMemoryOtelReader

_TRACE = "0af7651916cd43dd8448eb211c80319c"


@pytest.fixture
def reader(exporter: InMemorySpanExporter) -> InMemoryOtelReader:
    return InMemoryOtelReader(exporter)


@pytest.mark.parametrize("kind", list(SpanKind))
async def test_every_kind_round_trips(writer: OtelWriter, reader: InMemoryOtelReader, kind: SpanKind):
    with writer.trace_attributes(name="t"):
        span = writer.open_span(
            name="r",
            kind=kind,
            trace_context=TraceContext(trace_id=_TRACE),
            input_={"in": [1, 2]},
            model="req",
            metadata={"k": "v", STEP_ROLE_METADATA_KEY: StepRole.SUB_STEP, TIMING_METADATA_KEY: "absent"},
        )
        span.update(
            output={"out": True},
            model="resp",
            usage=TokenUsage(input_tokens=1, output_tokens=2, total_tokens=3, cost_usd=0.5),
            level=MonitoringLevel.WARNING,
            status_message="careful",
        )
        span.end()
    writer.flush()
    obs = await reader.get_observation(_TRACE, span.id)
    assert obs.kind is kind
    assert obs.name == "r"
    assert obs.trace_id == _TRACE
    assert obs.parent_id is None
    assert obs.input == {"in": [1, 2]}
    assert obs.output == {"out": True}
    assert obs.metadata == {"k": "v", "tai42.step_role": "sub_step", "tai42.timing": "absent"}
    assert (obs.input_tokens, obs.output_tokens, obs.total_tokens) == (1, 2, 3)
    assert obs.model == "resp"
    assert obs.level == "WARNING"
    assert obs.status_message == "careful"
    assert obs.start is not None
    assert obs.end is not None


async def test_error_status_round_trips(writer: OtelWriter, reader: InMemoryOtelReader):
    writer.record_span(
        name="e",
        kind=SpanKind.TOOL,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        trace_context=TraceContext(trace_id=_TRACE),
        level=MonitoringLevel.ERROR,
        status_message="broke",
    )
    writer.flush()
    (obs,) = (await reader.get_trace(_TRACE)).observations
    assert (obs.level, obs.status_message) == ("ERROR", "broke")
    assert obs.start == datetime(2026, 1, 1, tzinfo=UTC)


async def test_trace_and_parentage(writer: OtelWriter, reader: InMemoryOtelReader):
    with writer.start_span(
        name="root", kind=SpanKind.CHAIN, trace_context=TraceContext(trace_id=_TRACE), input_="q"
    ) as root:
        writer.open_span(name="child", kind=SpanKind.TOOL).end()
        writer.update_current_span(output="a")
    writer.flush()
    trace = await reader.get_trace(_TRACE)
    assert [o.name for o in trace.observations] == ["root", "child"]
    assert trace.observations[1].parent_id == root.id
    assert (trace.input, trace.output) == ("q", "a")
    assert trace.observations[1].metadata is None


async def test_absent_trace_and_observation(writer: OtelWriter, reader: InMemoryOtelReader):
    with pytest.raises(TraceNotFoundError):
        await reader.get_trace(_TRACE)
    writer.open_span(name="r", kind=SpanKind.CHAIN, trace_context=TraceContext(trace_id=_TRACE)).end()
    writer.flush()
    with pytest.raises(ObservationNotFoundError):
        await reader.get_observation(_TRACE, "0" * 16)
    with pytest.raises(TraceNotFoundError):
        await reader.get_observation("f" * 32, "0" * 16)


async def test_list_and_aggregate_reads_are_not_supported(reader: InMemoryOtelReader):
    now = datetime.now(UTC)
    assert reader.metrics_capability().measures == frozenset()
    assert reader.list_capability().sort_fields == frozenset()
    assert reader.max_page_size() >= 1
    with pytest.raises(MonitoringReadNotSupportedError):
        await reader.list_traces()
    with pytest.raises(MonitoringReadNotSupportedError):
        await reader.list_spans_in_window(now, now)
    with pytest.raises(MonitoringReadNotSupportedError):
        await reader.query_metrics(MetricsQuery(measures=[Measure.COUNT], from_timestamp=now, to_timestamp=now))
