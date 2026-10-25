"""The run reads: ``get_trace``, ``get_observation`` and the newest-first ``list_traces``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai42_contract.monitoring import (
    MonitoringError,
    MonitoringFilter,
    MonitoringReadNotServedError,
    MonitoringReadTooBroadError,
    ObservationNotFoundError,
    OrderBy,
    SpanKind,
    TraceNotFoundError,
)

from tai42_monitoring_langfuse.observation_query import EXPAND_METADATA

from ._fake_langfuse import FakeLangfuse

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
_LONG_METADATA = json.dumps({"k": "x" * 600})
_LONG_TIMING = json.dumps({"phases": ["p" * 40] * 8})


def _tool_run(fake: FakeLangfuse) -> None:
    fake.run(
        "t1",
        T0,
        seconds=2,
        tags=["a", "b"],
        trace_name="run",
        input='{"q": 1}',
        output="hello",
        metadata={"team": "blue"},
    )
    fake.add(
        id="tool",
        trace_id="t1",
        parent_id="t1-root",
        type="TOOL",
        name="echo",
        start=T0 + timedelta(milliseconds=5),
        end=T0 + timedelta(seconds=1),
        input='{"a": 1}',
        output="done",
        metadata={
            "attributes.tai42.metadata": _LONG_METADATA,
            "attributes.tai42.step_role": "tool-call",
            "attributes.tai42.timing": _LONG_TIMING,
        },
    )
    fake.add(
        id="llm",
        trace_id="t1",
        parent_id="t1-root",
        type="GENERATION",
        start=T0 + timedelta(milliseconds=10),
        level="ERROR",
        status_message="boom",
        model="m-1",
        usage={"input": 3, "output": 4, "total": 7},
        total_cost=0.25,
    )


async def test_get_trace_maps_every_observation(fake, reader):
    _tool_run(fake)
    trace = await reader.get_trace("t1")
    assert trace.id == "t1"
    assert trace.timestamp == T0
    assert trace.tags == ["a", "b"]
    assert trace.input == {"q": 1}
    assert trace.output == "hello"
    assert trace.metadata == {"team": "blue"}
    assert trace.total_cost == 0.25
    assert [o.id for o in trace.observations] == ["t1-root", "tool", "llm"]
    root, tool, llm = trace.observations
    assert root.parent_id is None
    assert root.kind is SpanKind.CHAIN
    assert tool.kind is SpanKind.TOOL
    assert tool.input == {"a": 1}
    assert tool.output == "done"
    assert llm.kind is SpanKind.LLM
    assert llm.level == "ERROR"
    assert llm.status_message == "boom"
    assert llm.model == "m-1"
    assert (llm.input_tokens, llm.output_tokens, llm.total_tokens) == (3, 4, 7)


async def test_get_trace_reads_long_producer_metadata_whole(fake, reader):
    _tool_run(fake)
    trace = await reader.get_trace("t1")
    tool = next(o for o in trace.observations if o.id == "tool")
    assert tool.metadata == {
        "k": "x" * 600,
        "tai42.step_role": "tool-call",
        "tai42.timing": json.loads(_LONG_TIMING),
    }
    assert all(kw["expandMetadata"] == EXPAND_METADATA for kw in fake.calls_of("observations"))


async def test_get_trace_scopes_only_by_the_trace_id(fake, reader):
    _tool_run(fake)
    await reader.get_trace("t1")
    assert fake.filters_sent() == [[{"column": "traceId", "operator": "=", "value": "t1", "type": "string"}]]


async def test_get_trace_with_no_rows_is_not_found(fake, reader):
    with pytest.raises(TraceNotFoundError, match="t-missing"):
        await reader.get_trace("t-missing")


async def test_get_trace_failure_propagates(fake, reader):
    timeout = httpx.ReadTimeout("read timed out")
    fake.fail(timeout)
    with pytest.raises(MonitoringError, match="GET /api/public/v2/observations") as caught:
        await reader.get_trace("t1")
    assert type(caught.value) is MonitoringError
    assert caught.value.__cause__ is timeout


async def test_get_trace_without_a_root_reads_the_earliest_start(fake, reader):
    fake.add(id="late", trace_id="t1", parent_id="gone", start=T0 + timedelta(seconds=3))
    fake.add(id="early", trace_id="t1", parent_id="gone", start=T0)
    trace = await reader.get_trace("t1")
    assert trace.timestamp == T0
    assert trace.tags == []
    assert trace.total_cost is None


async def test_get_observation_sends_only_the_filter(fake, reader):
    _tool_run(fake)
    observation = await reader.get_observation("t1", "tool")
    assert observation.id == "tool"
    assert observation.metadata is not None
    assert observation.metadata["k"] == "x" * 600
    assert fake.filters_sent() == [
        [
            {"column": "traceId", "operator": "=", "value": "t1", "type": "string"},
            {"column": "id", "operator": "=", "value": "tool", "type": "string"},
        ]
    ]


async def test_get_observation_absent_is_observation_not_found(fake, reader):
    _tool_run(fake)
    with pytest.raises(ObservationNotFoundError, match="nope"):
        await reader.get_observation("t1", "nope")


async def test_get_observation_of_an_absent_trace_is_trace_not_found(fake, reader):
    with pytest.raises(TraceNotFoundError, match="t-missing"):
        await reader.get_observation("t-missing", "tool")


async def test_get_observation_failure_propagates(fake, reader):
    timeout = httpx.ReadTimeout("read timed out")
    fake.fail(timeout)
    with pytest.raises(MonitoringError, match="GET /api/public/v2/observations") as caught:
        await reader.get_observation("t1", "tool")
    assert type(caught.value) is MonitoringError
    assert caught.value.__cause__ is timeout


async def test_list_traces_summarizes_a_run_from_its_root_and_every_observation(fake, reader):
    _tool_run(fake)
    fake.add(
        id="deferred",
        trace_id="t1",
        parent_id="t1-root",
        start=T0 + timedelta(seconds=1),
        end=T0 + timedelta(seconds=30),
        usage={"input": 1, "output": 2},
        total_cost=0.5,
    )
    [summary] = await reader.list_traces(from_timestamp=T0 - timedelta(hours=1))
    assert summary.id == "t1"
    assert summary.timestamp == T0
    assert summary.name == "run"
    assert summary.tags == ["a", "b"]
    assert summary.input == {"q": 1}
    assert summary.output == "hello"
    assert summary.latency_ms == 2000.0
    assert summary.total_cost == 0.75
    assert summary.total_tokens == 10
    assert summary.status == "error"


async def test_a_run_with_no_usage_lists_none_totals_and_ok(fake, reader):
    fake.run("t1", T0)
    [summary] = await reader.list_traces()
    assert summary.total_cost is None
    assert summary.total_tokens is None
    assert summary.status == "ok"
    assert summary.name is None


async def test_a_run_still_open_lists_no_latency(fake, reader):
    fake.add(id="r", trace_id="t1", start=T0, name="open")
    [summary] = await reader.list_traces()
    assert summary.latency_ms is None
    assert summary.name == "open"


async def test_native_list_walks_roots_with_the_run_level_clauses(fake, reader):
    fake.run("t1", T0, seconds=3, trace_name="run", user_id="alice", tags=["a", "b"])
    page = await reader.list_traces(
        from_timestamp=T0 - timedelta(hours=1),
        to_timestamp=T0 + timedelta(hours=1),
        filter_=MonitoringFilter(
            name="run",
            user_id="alice",
            session_id="s-1",
            version="v-1",
            tags=["a", "b"],
            metadata={"team": "blue"},
            min_latency=2.5,
            max_latency=10.0,
        ),
    )
    assert page == []
    walk = fake.filters_sent()[0]
    assert walk == [
        {"column": "environment", "operator": "any of", "value": ["tai"], "type": "stringOptions"},
        {"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"},
        {"column": "startTime", "operator": ">=", "value": (T0 - timedelta(hours=1)).isoformat(), "type": "datetime"},
        {"column": "startTime", "operator": "<", "value": (T0 + timedelta(hours=1)).isoformat(), "type": "datetime"},
        {"column": "traceName", "operator": "=", "value": "run", "type": "string"},
        {"column": "userId", "operator": "=", "value": "alice", "type": "string"},
        {"column": "sessionId", "operator": "=", "value": "s-1", "type": "string"},
        {"column": "version", "operator": "=", "value": "v-1", "type": "string"},
        {"column": "traceTags", "operator": "all of", "value": ["a", "b"], "type": "arrayOptions"},
        {"column": "metadata", "type": "stringObject", "key": "team", "operator": "=", "value": "blue"},
        {"column": "latency", "operator": ">=", "value": 2.5, "type": "number"},
        {"column": "latency", "operator": "<=", "value": 10.0, "type": "number"},
    ]


async def test_native_list_filters_by_latency_on_the_root(fake, reader):
    fake.run("slow", T0, seconds=3)
    fake.run("fast", T0 + timedelta(seconds=1), seconds=1)
    page = await reader.list_traces(filter_=MonitoringFilter(min_latency=2.0))
    assert [s.id for s in page] == ["slow"]


async def test_tags_match_every_tag_asked(fake, reader):
    fake.run("both", T0, tags=["a", "b"])
    fake.run("one", T0 + timedelta(seconds=1), tags=["a"])
    page = await reader.list_traces(filter_=MonitoringFilter(tags=["a", "b"]))
    assert [s.id for s in page] == ["both"]


async def test_source_scopes_every_list_read(fake, reader):
    fake.run("ours", T0)
    fake.run("theirs", T0, environment="other")
    assert [s.id for s in await reader.list_traces()] == ["ours"]


async def test_native_list_follows_the_cursor_and_pages(fake, reader):
    for n in range(250):
        fake.run(f"t{n:03}", T0 + timedelta(seconds=n))
    first = await reader.list_traces(limit=100, page=1)
    third = await reader.list_traces(limit=100, page=3)
    assert [s.id for s in first] == [f"t{n:03}" for n in range(249, 149, -1)]
    assert [s.id for s in third] == [f"t{n:03}" for n in range(49, -1, -1)]


async def test_native_list_stops_at_its_reach(fake, reader):
    for n in range(2500):
        fake.add(id=f"r{n}", trace_id=f"t{n:04}", start=T0 + timedelta(seconds=n))
    await reader.list_traces(limit=10, page=1)
    walks = [kw for kw in fake.calls_of("observations") if "metrics" in kw["fields"]]
    assert len(walks) == 1


async def test_roots_tied_on_start_list_once_each_in_trace_id_order(fake, reader):
    for n in range(999):
        fake.add(id=f"r{n}", trace_id=f"late{n:03}", start=T0 + timedelta(hours=1, seconds=n))
    for trace_id in ("tie-c", "tie-a", "tie-b"):
        fake.add(id=f"{trace_id}-root", trace_id=trace_id, start=T0)
    first = await reader.list_traces(limit=100, page=10)
    second = await reader.list_traces(limit=100, page=11)
    assert [s.id for s in first][-1:] == ["tie-c"]
    assert [s.id for s in second] == ["tie-b", "tie-a"]


async def test_rows_out_of_start_order_fail_loudly(fake, reader):
    fake.run("t1", T0)
    fake.run("t2", T0 + timedelta(seconds=1))
    fake.out_of_order = True
    with pytest.raises(MonitoringError, match="out of startTime order"):
        await reader.list_traces()


async def test_paging_past_run_5000_is_too_broad(fake, reader):
    with pytest.raises(MonitoringReadTooBroadError, match="Paging past run 5,000 is not served"):
        await reader.list_traces(limit=100, page=51)
    assert fake.calls == []


async def test_export_reads_past_the_paging_bound(fake, reader):
    for n in range(5100):
        fake.add(id=f"r{n}", trace_id=f"t{n:04}", start=T0 + timedelta(seconds=n))
    rows = await reader.export_traces(max_rows=5000)
    assert len(rows) == 5001
    assert rows[0].id == "t5099"


async def test_export_of_nothing_is_empty(fake, reader):
    assert await reader.export_traces(max_rows=10) == []


async def test_export_with_a_negative_bound_raises(reader):
    with pytest.raises(ValueError, match="max_rows -1"):
        await reader.export_traces(max_rows=-1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 101}, "limit 101 exceeds the reader's maximum page size 100"),
        ({"limit": 0}, "limit 0 must be 1 or more"),
        ({"page": 0}, "page 0 must be 1 or more"),
    ],
)
async def test_bad_paging_raises_before_any_read(fake, reader, kwargs, message):
    with pytest.raises(ValueError, match=message):
        await reader.list_traces(**kwargs)
    assert fake.calls == []


async def test_an_unknown_sort_is_not_served(fake, reader):
    with pytest.raises(MonitoringReadNotServedError, match="cannot sort on 'bogus'"):
        await reader.list_traces(order_by=OrderBy(field="bogus"))


def test_list_capability_serves_every_sort_with_every_filter(reader):
    capability = reader.list_capability()
    assert capability.sort_fields == {"timestamp", "total_cost", "name", "id", "latency", "total_tokens"}
    assert capability.incompatible_filters == {}


def test_max_page_size_is_the_page_ceiling(reader):
    assert reader.max_page_size() == 100
