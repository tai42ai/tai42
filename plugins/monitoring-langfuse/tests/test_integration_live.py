"""Live tests against a real Langfuse 4 server, through the writer and the reader the plugin ships.

Run with ``pytest -m integration``. The reader's key pair comes from ``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST``, and the writer exports to ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``, a
collector in front of that Langfuse; the suite skips when any is unset. Every run here is written under a fresh
source, so the reads see only what this module wrote. Ingestion is asynchronous: each read polls until the written
runs are visible.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.monitoring import (
    TIMING_METADATA_KEY,
    Measure,
    MetricsQuery,
    MonitoringFilter,
    MonitoringLevel,
    OrderBy,
    SpanKind,
    TokenUsage,
    TraceNotFoundError,
)

from tai42_monitoring_langfuse import LangfuseMonitoring
from tai42_monitoring_langfuse.filters import environment_clause, trace_ids_clause
from tai42_monitoring_langfuse.project import LangfuseProject

pytestmark = pytest.mark.integration

_ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
_TAG = "live-check"
_COSTS = (0.03, 0.01, 0.02)
_VISIBLE_WITHIN = 120.0
# Langfuse keeps times to the millisecond: each run lasts long enough to have a latency and a start of its own.
_STEP_SECONDS = 0.05
# A producer metadata value longer than the 200 characters the server keeps unless the read expands it.
_LONG_METADATA = {"note": "m" * 588}
_LONG_TIMING = {"started": "2026-01-01T10:00:00Z", "spans": ["t" * 40 for _ in range(6)]}


def _backend(source: str) -> LangfuseMonitoring:
    creds = {key: os.environ.get(key, "") for key in _ENV_KEYS}
    if not all(creds.values()):
        pytest.skip(f"set {', '.join(_ENV_KEYS)} to run the live Langfuse tests")
    project = LangfuseProject(
        public_key=creds["LANGFUSE_PUBLIC_KEY"],
        secret_key=creds["LANGFUSE_SECRET_KEY"],
        host=creds["LANGFUSE_HOST"],
        source=source,
    )
    return LangfuseMonitoring(project=project)


@dataclass
class _Written:
    backend: LangfuseMonitoring
    source: str
    trace_ids: list[str]
    session_id: str
    since: datetime


def _write_run(backend: LangfuseMonitoring, session_id: str, cost: float, *, failed: bool) -> str:
    writer = backend.writer
    with (
        writer.trace_attributes(name="live-run", tags=[_TAG], user_id="alice", session_id=session_id),
        writer.start_span(name="run", kind=SpanKind.CHAIN, input_={"q": "ping"}) as root,
    ):
        with writer.start_span(
            name="echo",
            kind=SpanKind.TOOL,
            input_={"x": 1},
            metadata={**_LONG_METADATA, TIMING_METADATA_KEY: _LONG_TIMING},
        ) as tool:
            time.sleep(_STEP_SECONDS)
            tool.update(output="ok", level=MonitoringLevel.ERROR if failed else None)
        with writer.start_span(name="llm", kind=SpanKind.LLM, model="m-1", input_="hi") as llm:
            llm.update(output="hello", usage=TokenUsage(input_tokens=3, output_tokens=4, cost_usd=cost))
        root.update(output={"a": "pong"})
        trace_id = writer.current_trace_id()
    assert trace_id is not None
    return trace_id


@pytest.fixture(scope="module")
def written() -> Iterator[_Written]:
    with pytest.MonkeyPatch.context() as patch:
        # The writer refuses to start unless the span processor counts its queue-full drops.
        patch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
        source = f"live-{uuid.uuid4().hex[:12]}"
        backend = _backend(source)
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        since = datetime.now(UTC) - timedelta(minutes=1)
        trace_ids = [_write_run(backend, session_id, cost, failed=n == 1) for n, cost in enumerate(_COSTS)]
        backend.writer.flush()
        yield _Written(backend, source, trace_ids, session_id, since)


async def _eventually[T](read: Callable[[], Awaitable[T]], done: Callable[[T], bool]) -> T:
    deadline = asyncio.get_running_loop().time() + _VISIBLE_WITHIN
    while True:
        value = await read()
        if done(value) or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(2)


def _window(written: _Written) -> tuple[datetime, datetime]:
    return written.since, datetime.now(UTC) + timedelta(minutes=1)


async def _all_visible(written: _Written) -> None:
    reader = written.backend.reader
    want = set(written.trace_ids)
    seen = await _eventually(
        lambda: reader.list_traces(from_timestamp=written.since),
        lambda page: {s.id for s in page} == want and all(s.total_cost is not None for s in page),
    )
    assert {s.id for s in seen} == want


def _observations_of(
    written: _Written, trace_ids: list[str], *, fields: str = "core", source: str | None = None
) -> list[dict[str, Any]]:
    """The observations of ``trace_ids`` as the server answers them, read with one ``traceId any of`` clause."""
    clauses = [trace_ids_clause(trace_ids)]
    if source is not None:
        clauses.insert(0, environment_clause(source))
    client = written.backend._manager.active_client()
    response = client.get(
        "/api/public/v2/observations", params={"fields": fields, "filter": json.dumps(clauses), "limit": 100}
    )
    response.raise_for_status()
    return response.json()["data"]


async def test_trace_id_any_of_returns_exactly_the_rows_of_the_listed_traces(written):
    await _all_visible(written)
    first, second, _ = written.trace_ids
    rows = _observations_of(written, [first, second, "0" * 32])
    assert {row["traceId"] for row in rows} == {first, second}
    assert len(rows) == 6


async def test_a_run_reads_back_with_its_producer_metadata_whole(written):
    await _all_visible(written)
    trace = await written.backend.reader.get_trace(written.trace_ids[0])
    by_name = {o.name: o for o in trace.observations}
    assert set(by_name) == {"run", "echo", "llm"}
    tool = by_name["echo"]
    assert tool.kind is SpanKind.TOOL
    assert tool.metadata == {**_LONG_METADATA, TIMING_METADATA_KEY: _LONG_TIMING}
    assert len(json.dumps(_LONG_METADATA)) >= 600
    assert len(json.dumps(_LONG_TIMING)) > 200
    assert tool.input == {"x": 1}
    assert tool.output == "ok"
    llm = by_name["llm"]
    assert (llm.kind, llm.model, llm.input_tokens, llm.output_tokens) == (SpanKind.LLM, "m-1", 3, 4)
    assert trace.total_cost == pytest.approx(0.03)
    assert trace.tags == [_TAG]


async def test_every_observation_carries_the_run_attribution(written):
    await _all_visible(written)
    rows = _observations_of(written, written.trace_ids, fields="core,basic,trace_context", source=written.source)
    assert len(rows) == 9
    for row in rows:
        assert (row["traceName"], row["tags"], row["userId"], row["sessionId"]) == (
            "live-run",
            [_TAG],
            "alice",
            written.session_id,
        )


async def test_the_runs_list_summarises_each_run_from_its_root(written):
    await _all_visible(written)
    page = await written.backend.reader.list_traces(from_timestamp=written.since)
    assert [s.id for s in page] == list(reversed(written.trace_ids))
    by_id = {s.id: s for s in page}
    first = by_id[written.trace_ids[0]]
    assert (first.name, first.tags, first.input, first.output) == ("live-run", [_TAG], {"q": "ping"}, {"a": "pong"})
    assert first.latency_ms is not None
    assert first.latency_ms >= 0
    assert first.total_tokens == 7
    assert [by_id[t].status for t in written.trace_ids] == ["ok", "error", "ok"]


async def test_a_cost_rank_reads_the_ranked_runs(written):
    await _all_visible(written)
    page = await written.backend.reader.list_traces(
        from_timestamp=written.since, order_by=OrderBy(field="total_cost", direction="desc")
    )
    assert [s.total_cost for s in page] == pytest.approx(sorted(_COSTS, reverse=True))


async def test_a_latency_rank_and_a_run_wide_filter_read_the_window(written):
    await _all_visible(written)
    reader = written.backend.reader
    by_latency = await reader.list_traces(from_timestamp=written.since, order_by=OrderBy(field="latency"))
    latencies = [s.latency_ms for s in by_latency]
    assert latencies == sorted(latencies, reverse=True)
    failed = await reader.list_traces(
        from_timestamp=written.since,
        filter_=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=0.005, model="m-1", tags=[_TAG]),
    )
    assert [s.id for s in failed] == [written.trace_ids[1]]


async def test_the_native_filters_select_on_the_server(written):
    await _all_visible(written)
    reader = written.backend.reader
    for flt in (
        MonitoringFilter(name="live-run"),
        MonitoringFilter(user_id="alice"),
        MonitoringFilter(session_id=written.session_id),
        MonitoringFilter(tags=[_TAG]),
    ):
        page = await reader.list_traces(from_timestamp=written.since, filter_=flt)
        assert {s.id for s in page} == set(written.trace_ids), flt
    assert await reader.list_traces(from_timestamp=written.since, filter_=MonitoringFilter(name="absent")) == []


async def test_export_returns_every_run_of_the_range(written):
    await _all_visible(written)
    rows = await written.backend.reader.export_traces(from_timestamp=written.since, max_rows=10)
    assert {r.id for r in rows} == set(written.trace_ids)


async def test_the_step_window_lists_the_steps_with_their_metadata(written):
    await _all_visible(written)
    items = await written.backend.reader.list_spans_in_window(
        *_window(written), run=written.trace_ids[0], kind=SpanKind.TOOL
    )
    [tool] = items
    assert tool.name == "echo"
    assert tool.tags == [_TAG]
    assert tool.metadata is not None
    assert tool.metadata["attributes.tai42.metadata"] == _LONG_METADATA


async def test_metrics_count_runs_and_sum_usage(written):
    await _all_visible(written)
    start, end = _window(written)
    result = await written.backend.reader.query_metrics(
        MetricsQuery(
            measures=[Measure.COUNT, Measure.LATENCY, Measure.COST, Measure.TOKENS],
            from_timestamp=start,
            to_timestamp=end,
        )
    )
    [row] = result.rows
    assert row.measures[Measure.COUNT] == 3
    assert row.measures[Measure.COST] == pytest.approx(sum(_COSTS))
    assert row.measures[Measure.TOKENS] == 21
    assert row.measures[Measure.LATENCY] is not None


async def test_get_observation_tells_an_absent_trace_from_an_absent_observation(written):
    await _all_visible(written)
    reader = written.backend.reader
    with pytest.raises(TraceNotFoundError):
        await reader.get_observation("0" * 32, "0" * 16)
    tool = next(o for o in (await reader.get_trace(written.trace_ids[0])).observations if o.name == "echo")
    assert (await reader.get_observation(written.trace_ids[0], tool.id)).id == tool.id


async def test_a_source_reads_only_its_own_runs(written):
    await _all_visible(written)
    foreign_source = f"live-{uuid.uuid4().hex[:12]}"
    foreign = _backend(foreign_source)
    assert await foreign.reader.list_traces(from_timestamp=written.since) == []
    assert await foreign.reader.list_spans_in_window(*_window(written)) == []
    assert _observations_of(written, written.trace_ids, source=foreign_source) == []


def test_shutdown_then_emit_rebuilds(written):
    writer = written.backend.writer
    writer.shutdown()
    with writer.start_span(name="after-shutdown", kind=SpanKind.CHAIN):
        pass
    writer.flush()
    assert writer.export_health().records_failed == 0
