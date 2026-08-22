"""Live integration tests against a real Langfuse server.

Run with ``pytest -m integration``. Credentials come from the ``LANGFUSE_*``
environment; the suite skips cleanly when any is unset. Langfuse ingestion is
asynchronous, so the reader assertions verify the API calls parse — they do not
require the just-emitted span to already be queryable.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.monitoring import (
    MetricsFilter,
    MetricsView,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    ProjectConfig,
    SpanKind,
)

from tai42_monitoring_langfuse import LangfuseMonitoring

pytestmark = pytest.mark.integration

_SMOKE_TAG = "tai-monitoring-contract-smoke"

_ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


def _creds() -> dict[str, str]:
    creds = {key: os.environ.get(key, "") for key in _ENV_KEYS}
    if not all(creds.values()):
        pytest.skip("LANGFUSE_* credentials not available")
    return creds


def _backend_with_source(source: str = "tai") -> LangfuseMonitoring:
    creds = _creds()
    cfg = ProjectConfig(
        public_key=creds["LANGFUSE_PUBLIC_KEY"],
        secret_key=creds["LANGFUSE_SECRET_KEY"],
        host=creds["LANGFUSE_HOST"],
        source=source,
    )
    return LangfuseMonitoring(projects=[cfg], default_public_key=cfg.public_key)


@pytest.fixture(scope="module")
def backend() -> LangfuseMonitoring:
    return _backend_with_source()


def test_writer_emits_and_flushes(backend):
    writer = backend.writer
    with writer.trace_attributes(name="contract-smoke", tags=[_SMOKE_TAG]):
        with writer.start_span(name="smoke_node", kind=SpanKind.TOOL, input={"ping": 1}) as span:
            span.update(output={"pong": 2}, usage_details={"input": 1, "output": 1})
        writer.create_event(name="smoke_event", input={"e": 1}, output={"e": 2})
        tid = writer.current_trace_id()
    writer.flush()
    assert tid is None or isinstance(tid, str)


async def test_reader_query_metrics_executes(backend):
    now = datetime.now(UTC)
    result = await backend.reader.query_metrics(
        MetricsFilter(
            view=MetricsView.TRACES,
            metrics=["count"],
            from_timestamp=now - timedelta(hours=1),
            to_timestamp=now + timedelta(minutes=1),
            granularity="day",
        )
    )
    assert result.rows is not None  # parsed without error


async def test_reader_list_spans_in_window_executes(backend):
    now = datetime.now(UTC)
    items = await backend.reader.list_spans_in_window(
        now - timedelta(hours=1),
        now + timedelta(minutes=1),
    )
    assert isinstance(items, list)


def test_shutdown_then_emit_rebuilds(backend):
    backend.writer.shutdown()
    # First use after shutdown must rebuild a clean client and not raise.
    with backend.writer.start_span(name="post_shutdown", kind=SpanKind.CHAIN):
        pass
    backend.writer.flush()


async def test_reader_list_and_get_trace_execute(backend):
    now = datetime.now(UTC)
    traces = await backend.reader.list_traces(from_timestamp=now - timedelta(hours=1), limit=3)
    assert isinstance(traces, list)
    if not traces:
        pytest.skip("no trace in the last hour to round-trip get_trace")
    first = traces[0]
    assert isinstance(first, MonitoringTraceSummary)
    full = await backend.reader.get_trace(first.id)
    assert isinstance(full, MonitoringTrace)
    assert full.id == first.id


# --- filter columns + sort: every clause must round-trip the server without a
# 4xx (a wrong column id raises); a parsed list back is enough. ----------------


def _trace_filters():
    return {
        "name": MonitoringFilter(name="contract-smoke"),
        "level": MonitoringFilter(level=MonitoringLevel.ERROR),
        "tags": MonitoringFilter(tags=[_SMOKE_TAG]),
        "metadata": MonitoringFilter(metadata={"k": "v"}),
        "cost": MonitoringFilter(min_cost=0.0, max_cost=1000.0),
        "tokens": MonitoringFilter(min_tokens=0, max_tokens=10_000_000),
        "latency": MonitoringFilter(min_latency=0.0, max_latency=100000.0),
    }


@pytest.mark.parametrize("key", list(_trace_filters()))
async def test_list_traces_filter_columns_execute(backend, key):
    now = datetime.now(UTC)
    result = await backend.reader.list_traces(
        from_timestamp=now - timedelta(days=7), limit=3, filter=_trace_filters()[key]
    )
    assert isinstance(result, list)


@pytest.mark.parametrize("field", ["timestamp", "name", "id", "total_cost", "latency", "total_tokens"])
async def test_list_traces_sort_fields_execute(backend, field):
    now = datetime.now(UTC)
    result = await backend.reader.list_traces(
        from_timestamp=now - timedelta(days=7), limit=3, order_by=OrderBy(field=field)
    )
    assert isinstance(result, list)


async def test_list_traces_cost_sort_monotonic(backend):
    # total_cost is the only metric sort whose value is returned, so its ordering
    # is the one we can assert.
    now = datetime.now(UTC)
    result = await backend.reader.list_traces(
        order_by=OrderBy(field="total_cost", direction="desc"),
        from_timestamp=now - timedelta(days=14),
        limit=5,
    )
    costs = [t.total_cost for t in result if t.total_cost is not None]
    if len(costs) < 2:
        pytest.skip("need >=2 comparable cost traces to assert an order")
    assert costs == sorted(costs, reverse=True)


async def test_list_traces_metric_sort_tags_filter_round_trip(backend):
    # Proves the tags arrayOptions clause round-trips on the metrics traces view
    # (a vendor-fragile filter surface) without a 4xx.
    now = datetime.now(UTC)
    result = await backend.reader.list_traces(
        order_by=OrderBy(field="total_cost", direction="desc"),
        from_timestamp=now - timedelta(days=14),
        limit=5,
        filter=MonitoringFilter(tags=[_SMOKE_TAG]),
    )
    assert isinstance(result, list)


def _span_filters():
    f = dict(_trace_filters())
    # model is a span/observation-only column (no trace column).
    f["model"] = MonitoringFilter(model="gpt-4")
    return f


@pytest.mark.parametrize("key", list(_span_filters()))
async def test_list_spans_filter_columns_execute(backend, key):
    now = datetime.now(UTC)
    items = await backend.reader.list_spans_in_window(
        now - timedelta(days=7), now + timedelta(minutes=1), filter=_span_filters()[key]
    )
    assert isinstance(items, list)


async def test_list_spans_session_resolution_executes(backend):
    now = datetime.now(UTC)
    items = await backend.reader.list_spans_in_window(
        now - timedelta(days=7),
        now + timedelta(minutes=1),
        filter=MonitoringFilter(session_id="no-such-session"),
    )
    assert isinstance(items, list)


@pytest.mark.parametrize("field", ["start", "end", "duration", "name", "id"])
async def test_list_spans_sort_fields_execute(backend, field):
    now = datetime.now(UTC)
    items = await backend.reader.list_spans_in_window(
        now - timedelta(days=7),
        now + timedelta(minutes=1),
        order_by=OrderBy(field=field),
    )
    assert isinstance(items, list)


# These assert the SEMANTICS round-trip on the real server (not just HTTP-200):
# source isolation actually filters, and the sort actually orders.


async def test_source_scoping_excludes_foreign_environment():
    # A backend scoped to a source nothing was written under returns nothing —
    # proving the reader applies the source filter, not that it just doesn't 4xx.
    foreign = _backend_with_source("zzz-no-such-source-xyz")
    now = datetime.now(UTC)
    assert await foreign.reader.list_traces(from_timestamp=now - timedelta(days=30), limit=5) == []
    assert await foreign.reader.list_spans_in_window(now - timedelta(days=30), now + timedelta(minutes=1)) == []


async def test_list_traces_returned_order_is_newest_first(backend):
    now = datetime.now(UTC)
    traces = await backend.reader.list_traces(
        from_timestamp=now - timedelta(days=30),
        limit=10,
        order_by=OrderBy(field="timestamp", direction="desc"),
    )
    stamps = [t.timestamp for t in traces if t.timestamp is not None]
    if len(stamps) < 2:
        pytest.skip("need >=2 timestamped traces to assert an order")
    assert stamps == sorted(stamps, reverse=True)


async def test_list_spans_returned_order_is_start_desc(backend):
    now = datetime.now(UTC)
    items = await backend.reader.list_spans_in_window(
        now - timedelta(days=30),
        now + timedelta(minutes=1),
        order_by=OrderBy(field="start", direction="desc"),
    )
    starts = [i.start for i in items if i.start is not None]
    if len(starts) < 2:
        pytest.skip("need >=2 spans to assert an order")
    assert starts == sorted(starts, reverse=True)
