"""The ranked ``list_traces`` read: a cost or token rank answered by one top-N metrics query."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.monitoring import MonitoringError, MonitoringFilter, MonitoringReadTooBroadError, OrderBy

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
FROM = T0 - timedelta(hours=1)
_COST_DESC = OrderBy(field="total_cost", direction="desc")


def _metric_queries(fake):
    return [json.loads(kw["query"]) for kw in fake.calls_of("metrics")]


def _walks(fake):
    return [kw for kw in fake.calls_of("observations") if kw["fields"] == "core,basic,metrics,trace_context"]


async def test_cost_rank_is_one_top_n_query_and_no_window_walk(fake, reader):
    fake.run("cheap", T0, children=1, child_cost=0.1)
    fake.run("dear", T0 + timedelta(seconds=1), children=2, child_cost=0.5)
    fake.run("none", T0 + timedelta(seconds=2))
    page = await reader.list_traces(from_timestamp=FROM, to_timestamp=T0 + timedelta(hours=1), order_by=_COST_DESC)
    assert [s.id for s in page] == ["dear", "cheap", "none"]
    assert [s.total_cost for s in page] == [1.0, 0.1, None]
    [query] = _metric_queries(fake)
    assert query == {
        "view": "observations",
        "dimensions": [{"field": "traceId"}],
        "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
        "filters": [{"column": "environment", "operator": "any of", "value": ["tai"], "type": "stringOptions"}],
        "fromTimestamp": FROM.isoformat(),
        "toTimestamp": (T0 + timedelta(hours=1)).isoformat(),
        "orderBy": [{"field": "sum_totalCost", "direction": "desc"}],
        "config": {"row_limit": 1000},
    }
    root_fetches = [json.loads(kw["filter"]) for kw in _walks(fake)]
    assert len(root_fetches) == 1
    assert root_fetches[0][2]["column"] == "traceId"


async def test_token_rank_sums_total_tokens(fake, reader):
    fake.run("small", T0, children=1, child_tokens=5)
    fake.run("big", T0 + timedelta(seconds=1), children=3, child_tokens=10)
    page = await reader.list_traces(from_timestamp=FROM, order_by=OrderBy(field="total_tokens", direction="desc"))
    assert [(s.id, s.total_tokens) for s in page] == [("big", 30), ("small", 5)]
    assert _metric_queries(fake)[0]["metrics"] == [{"measure": "totalTokens", "aggregation": "sum"}]


async def test_ranked_query_sends_the_name_and_every_tag(fake, reader):
    fake.run("both", T0, children=1, child_cost=0.2, tags=["a", "b"], trace_name="run")
    fake.run("one", T0, children=1, child_cost=0.9, tags=["a"], trace_name="run")
    page = await reader.list_traces(
        from_timestamp=FROM, order_by=_COST_DESC, filter_=MonitoringFilter(name="run", tags=["a", "b"])
    )
    assert [s.id for s in page] == ["both"]
    assert _metric_queries(fake)[0]["filters"][1:] == [
        {"column": "traceName", "operator": "=", "value": "run", "type": "string"},
        {"column": "tags", "operator": "all of", "value": ["a", "b"], "type": "arrayOptions"},
    ]


async def test_ranked_ties_break_by_trace_id(fake, reader):
    for trace_id in ("b", "c", "a"):
        fake.run(trace_id, T0, children=1, child_cost=0.5)
    page = await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC)
    assert [s.id for s in page] == ["c", "b", "a"]


async def test_ranked_pages_slice_the_rank(fake, reader):
    for n in range(25):
        fake.run(f"t{n:02}", T0, children=1, child_cost=float(n))
    page = await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC, limit=10, page=2)
    assert [s.id for s in page] == [f"t{n:02}" for n in range(14, 4, -1)]


async def test_ranked_reads_roots_in_rank_order_until_the_page_is_full(fake, reader):
    for n in range(1000):
        fake.run(f"t{n:04}", T0, children=1, child_cost=float(n))
    page = await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC, limit=50)
    assert [s.id for s in page] == [f"t{n:04}" for n in range(999, 949, -1)]
    [root_fetch] = [json.loads(kw["filter"]) for kw in _walks(fake)]
    assert root_fetch[2]["value"] == [f"t{n:04}" for n in range(999, 899, -1)]


async def test_ranked_reads_on_past_a_chunk_whose_roots_are_outside_the_range(fake, reader):
    for n in range(100):
        early = fake.run(f"e{n:03}", FROM - timedelta(minutes=1), seconds=7200)
        fake.add(id=f"e{n:03}-late", trace_id=early.trace_id, parent_id=early.id, start=T0, total_cost=100.0 + n)
    for n in range(150):
        fake.run(f"t{n:03}", T0, children=1, child_cost=float(n))
    page = await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC, limit=10, page=6)
    assert [s.id for s in page] == [f"t{n:03}" for n in range(99, 89, -1)]
    assert len(_walks(fake)) == 2


async def test_ranked_drops_a_run_whose_root_is_outside_the_range(fake, reader):
    fake.run("inside", T0, children=1, child_cost=0.1)
    early = fake.run("early", FROM - timedelta(minutes=5), seconds=3600)
    fake.add(id="late-child", trace_id="early", parent_id=early.id, start=T0, total_cost=5.0)
    fake.add(id="orphan", trace_id="rootless", parent_id="missing", start=T0, total_cost=9.0)
    page = await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC)
    assert [s.id for s in page] == ["inside"]


async def test_ranked_needs_a_start_time(fake, reader):
    with pytest.raises(MonitoringReadTooBroadError, match=r"A ranked list needs a start time\."):
        await reader.list_traces(order_by=_COST_DESC)
    assert fake.calls == []


async def test_ranked_reach_past_1000_is_too_broad(fake, reader):
    with pytest.raises(MonitoringReadTooBroadError, match="A ranked list pages up to run 1,000"):
        await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC, limit=100, page=11)
    assert fake.calls == []


async def test_a_full_rank_with_dropped_runs_and_a_short_page_is_too_broad(fake, reader):
    for n in range(1000):
        early = fake.run(f"e{n:04}", FROM - timedelta(minutes=1), seconds=7200)
        fake.add(id=f"e{n:04}-late", trace_id=early.trace_id, parent_id=early.id, start=T0, total_cost=1.0 + n)
    fake.run("kept", T0, children=1, child_cost=0.5)
    with pytest.raises(MonitoringReadTooBroadError, match="A ranked list pages up to run 1,000"):
        await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC, limit=10)


async def test_export_of_a_rank_reads_past_the_page_bound(fake, reader):
    for n in range(3):
        fake.run(f"t{n}", T0, children=1, child_cost=float(n + 1))
    rows = await reader.export_traces(from_timestamp=FROM, max_rows=2000, order_by=_COST_DESC)
    assert [s.id for s in rows] == ["t2", "t1", "t0"]


async def test_a_metrics_row_without_its_column_fails_loudly(fake, reader, monkeypatch):
    fake.run("t1", T0, children=1, child_cost=0.1)

    async def _bad(client, query):
        return [{"traceId": "t1"}]

    monkeypatch.setattr(reader._traces, "_metrics", _bad)
    with pytest.raises(MonitoringError, match="no traceId or sum_totalCost column"):
        await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC)


async def test_a_root_only_clause_ranks_in_the_window(fake, reader):
    fake.run("alice", T0, children=1, child_cost=0.1, user_id="alice")
    fake.run("bob", T0, children=1, child_cost=0.9, user_id="bob")
    page = await reader.list_traces(from_timestamp=FROM, order_by=_COST_DESC, filter_=MonitoringFilter(user_id="alice"))
    assert [s.id for s in page] == ["alice"]
    assert fake.calls_of("metrics") == []
