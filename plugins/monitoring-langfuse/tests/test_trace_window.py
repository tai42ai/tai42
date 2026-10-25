"""The window ``list_traces`` read: the time range's runs ranked or filtered in the plugin."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.monitoring import (
    MonitoringError,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringReadTooBroadError,
    OrderBy,
)

from ._fake_langfuse import Row

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
FROM = T0 - timedelta(hours=1)
_ROOT_FIELDS = "core,basic,metrics,trace_context"
_ENRICH_FIELDS = "core,basic,usage,model"


def _calls(fake, fields):
    return [kw for kw in fake.calls_of("observations") if kw["fields"] == fields]


def _many_runs(fake, count, *, children=0):
    for n in range(count):
        fake.run(f"t{n:04}", T0 + timedelta(seconds=n), seconds=(n % 97) + 1, children=children, child_cost=0.01)


async def test_a_latency_rank_reads_the_window_and_enriches_only_the_page(fake, reader):
    _many_runs(fake, 1500)
    page = await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="latency", direction="desc"), limit=5)
    assert [s.latency_ms for s in page] == [97000.0] * 5
    assert [s.id for s in page] == ["t1454", "t1357", "t1260", "t1163", "t1066"]
    assert len(_calls(fake, _ROOT_FIELDS)) == 2
    assert len(_calls(fake, "core,io")) == 1
    assert len(_calls(fake, _ENRICH_FIELDS)) == 1


async def test_a_cost_rank_lowest_first_enriches_every_candidate_and_stops_at_the_observation_cap(fake, reader):
    _many_runs(fake, 1500, children=33)
    with pytest.raises(MonitoringReadTooBroadError, match="more than 50,000 observations"):
        await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="total_cost", direction="asc"))
    enrichment = _calls(fake, _ENRICH_FIELDS)
    chunks = {json.dumps(json.loads(kw["filter"])[1]["value"]) for kw in enrichment}
    assert len(chunks) == 15


async def test_more_than_2000_roots_is_too_broad_naming_the_purpose(fake, reader):
    _many_runs(fake, 2001)
    with pytest.raises(
        MonitoringReadTooBroadError,
        match=re.escape(
            "This list reads at most 2,000 runs in the time range to rank by latency, lowest first, "
            "and this range holds more. Narrow the time range or add filters."
        ),
    ):
        await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="latency", direction="asc"))


@pytest.mark.parametrize(
    ("order_by", "filter_", "purpose"),
    [
        (OrderBy(field="name", direction="asc"), None, "rank by name"),
        (OrderBy(field="id", direction="desc"), None, "rank by run id"),
        (OrderBy(field="timestamp", direction="asc"), None, "rank by time, oldest first"),
        (OrderBy(field="total_tokens", direction="asc"), None, "rank by tokens, lowest first"),
        (None, MonitoringFilter(min_cost=0.1, level=MonitoringLevel.ERROR), "apply the cost, status filters"),
        (None, MonitoringFilter(model="m"), "apply the model filter"),
    ],
)
async def test_the_too_broad_answer_names_why_the_window_is_read(fake, reader, order_by, filter_, purpose):
    _many_runs(fake, 2001)
    with pytest.raises(MonitoringReadTooBroadError, match=f"to {purpose}, and this range holds more"):
        await reader.list_traces(from_timestamp=FROM, order_by=order_by, filter_=filter_)


async def test_a_window_read_needs_a_start_time(fake, reader):
    with pytest.raises(MonitoringReadTooBroadError, match=r"A ranked list needs a start time\."):
        await reader.list_traces(order_by=OrderBy(field="latency"))
    assert fake.calls == []


async def test_cost_rank_with_run_wide_filters_returns_the_filtered_page(fake, reader):
    fake.run("match-low", T0, children=2, child_cost=0.3, child_level="ERROR", child_model="m-1")
    fake.run(
        "match-high", T0 + timedelta(seconds=1), children=2, child_cost=0.6, child_level="ERROR", child_model="m-1"
    )
    fake.run("too-cheap", T0 + timedelta(seconds=2), children=1, child_cost=0.1, child_level="ERROR", child_model="m-1")
    fake.run("no-error", T0 + timedelta(seconds=3), children=2, child_cost=0.6, child_model="m-1")
    fake.run(
        "other-model", T0 + timedelta(seconds=4), children=2, child_cost=0.6, child_level="ERROR", child_model="m-2"
    )
    fake.run("no-cost", T0 + timedelta(seconds=5), children=2, child_level="ERROR", child_model="m-1")
    page = await reader.list_traces(
        from_timestamp=FROM,
        order_by=OrderBy(field="total_cost", direction="desc"),
        filter_=MonitoringFilter(min_cost=0.5, level=MonitoringLevel.ERROR, model="m-1"),
    )
    assert [(s.id, s.total_cost, s.status) for s in page] == [("match-high", 1.2, "error"), ("match-low", 0.6, "error")]
    assert fake.calls_of("metrics") == []


async def test_a_token_range_never_matches_a_run_without_tokens(fake, reader):
    fake.run("tokens", T0, children=1, child_tokens=10)
    fake.run("none", T0 + timedelta(seconds=1))
    page = await reader.list_traces(from_timestamp=FROM, filter_=MonitoringFilter(max_tokens=100))
    assert [s.id for s in page] == ["tokens"]


async def test_a_name_rank_puts_unnamed_runs_last(fake, reader):
    fake.run("b", T0, trace_name="beta")
    fake.run("a", T0 + timedelta(seconds=1), trace_name="alpha")
    fake.run("n", T0 + timedelta(seconds=2))
    asc = await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="name", direction="asc"))
    desc = await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="name", direction="desc"))
    assert [s.id for s in asc] == ["a", "b", "n"]
    assert [s.id for s in desc] == ["b", "a", "n"]


async def test_oldest_first_reads_the_window(fake, reader):
    fake.run("old", T0)
    fake.run("new", T0 + timedelta(seconds=1))
    page = await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="timestamp", direction="asc"))
    assert [s.id for s in page] == ["old", "new"]


async def test_export_of_a_window_read_walks_once(fake, reader):
    _many_runs(fake, 1500)
    rows = await reader.export_traces(from_timestamp=FROM, max_rows=1000, order_by=OrderBy(field="latency"))
    assert len(rows) == 1001
    assert len(_calls(fake, _ROOT_FIELDS)) == 2
    fake.calls.clear()
    await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="latency"))
    assert len(_calls(fake, _ROOT_FIELDS)) == 2


async def test_an_observation_of_an_unrequested_trace_fails_loudly(fake, reader):
    fake.run("t1", T0)
    fake.stray.append(Row(id="stray", trace_id="t9", parent_id="elsewhere", start=T0))
    with pytest.raises(MonitoringError, match="unrequested trace t9"):
        await reader.list_traces(from_timestamp=FROM, filter_=MonitoringFilter(min_cost=0.0))


async def test_a_cost_rank_lowest_first_puts_runs_without_cost_last(fake, reader):
    fake.run("dear", T0, children=1, child_cost=0.9)
    fake.run("none", T0 + timedelta(seconds=1))
    fake.run("cheap", T0 + timedelta(seconds=2), children=1, child_cost=0.1)
    page = await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="total_cost", direction="asc"))
    assert [(s.id, s.total_cost) for s in page] == [("cheap", 0.1), ("dear", 0.9), ("none", None)]
    assert fake.calls_of("metrics") == []
