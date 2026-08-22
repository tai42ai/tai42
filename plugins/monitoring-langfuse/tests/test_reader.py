"""Reader query building, filter mapping, sorting, and hydration semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langfuse.api.commons.errors import NotFoundError
from tai42_contract.monitoring import (
    MetricsFilter,
    MetricsView,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringReadNotSupportedError,
    OrderBy,
    SpanKind,
    TraceNotFoundError,
)

from tai42_monitoring_langfuse.reader import LangfuseReader


def _obs(**kw):
    base = {
        "id": "o",
        "trace_id": "t",
        "parent_observation_id": None,
        "type": "SPAN",
        "name": "node",
        "input": None,
        "output": None,
        "metadata": None,
        "start_time": datetime(2026, 1, 1, tzinfo=UTC),
        "end_time": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _obs_page(data, page=1, total_pages=1):
    return SimpleNamespace(data=data, meta=SimpleNamespace(page=page, total_pages=total_pages))


async def test_query_metrics_builds_query_and_parses_rows(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"name": "flow-a", "count": 3, "totalCost": 0.5}]
    )
    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    result = await reader.query_metrics(
        MetricsFilter(
            view=MetricsView.TRACES,
            metrics=["count", "totalCost"],
            dimensions=["name"],
            from_timestamp=now,
            to_timestamp=now,
            granularity="day",
        )
    )

    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert query["view"] == "traces"
    assert {"measure": "count", "aggregation": "count"} in query["metrics"]
    assert {"measure": "totalCost", "aggregation": "sum"} in query["metrics"]
    assert query["dimensions"] == [{"field": "name"}]
    assert query["timeDimension"] == {"granularity": "day"}

    assert len(result.rows) == 1
    assert result.rows[0].dimensions == {"name": "flow-a"}
    assert result.rows[0].metrics == {"count": 3, "totalCost": 0.5}


async def test_query_metrics_forwards_order_by_and_defaults_unknown_measure_to_sum(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await LangfuseReader(manager).query_metrics(
        MetricsFilter(
            metrics=["latency", "someNewMeasure"],
            from_timestamp=now,
            to_timestamp=now,
            order_by=[{"field": "latency", "direction": "asc"}],
        )
    )
    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert {"measure": "latency", "aggregation": "avg"} in query["metrics"]
    # A measure with no natural aggregation falls back to sum.
    assert {"measure": "someNewMeasure", "aggregation": "sum"} in query["metrics"]
    assert query["orderBy"] == [{"field": "latency", "direction": "asc"}]


async def test_query_metrics_distinct_tag_count(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"tags": "a"}, {"tags": "b"}, {"tags": "c"}]
    )
    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = await reader.query_metrics(
        MetricsFilter(metrics=["count"], dimensions=["tags"], from_timestamp=now, to_timestamp=now)
    )
    assert result.derived["distinct_tag_count"] == 3


async def test_list_spans_selects_tool_granularity(manager, mock_client):
    observations = [
        _obs(id="span1", type="SPAN", name="my_node"),
        _obs(id="tool1", type="TOOL", name="search"),
        _obs(id="gen1", type="GENERATION", name="llm"),  # nested LLM -> excluded
        _obs(id="evt1", type="EVENT", name="ToolCall:search"),  # event marker -> excluded
        _obs(id="grp1", type="SPAN", name="tools"),  # grouping chain -> excluded
        _obs(id="jq1", type="SPAN", name="condition:check"),  # jq sub-step -> excluded
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=["run:7"])

    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await reader.list_spans_in_window(now, now)

    ids = {i.id for i in items}
    assert ids == {"span1", "tool1"}
    assert all(i.tags == ["run:7"] for i in items)


async def test_list_spans_run_scope_and_kind_narrowing(manager, mock_client):
    observations = [
        _obs(id="tool1", type="TOOL", name="search"),
        _obs(id="agent1", type="AGENT", name="planner"),
        _obs(id="chain1", type="CHAIN", name="step"),
        _obs(id="retr1", type="RETRIEVER", name="lookup"),
        _obs(id="span1", type="SPAN", name="node"),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    # run= scopes to a single trace (forwarded as trace_id).
    await reader.list_spans_in_window(now, now, run="trace-7")
    assert mock_client.api.legacy.observations_v1.get_many.call_args.kwargs["trace_id"] == "trace-7"

    # No kind -> AGENT/CHAIN/RETRIEVER/TOOL/SPAN all kept (tool-granularity set).
    items = await reader.list_spans_in_window(now, now)
    assert {i.id for i in items} == {"tool1", "agent1", "chain1", "retr1", "span1"}

    # kind=TOOL narrows within the set to TOOL-typed observations only.
    items = await reader.list_spans_in_window(now, now, kind=SpanKind.TOOL)
    assert {i.id for i in items} == {"tool1"}


async def test_list_spans_failed_tag_fetch_degrades_to_empty(manager, mock_client):
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page([_obs(id="a", type="TOOL", name="x")])
    mock_client.api.trace.get.side_effect = RuntimeError("boom")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await LangfuseReader(manager).list_spans_in_window(now, now)
    assert [i.id for i in items] == ["a"]
    assert items[0].tags == []


async def test_list_spans_tag_filter_and_pagination(manager, mock_client):
    page1 = _obs_page([_obs(id="a", type="TOOL", name="x")], page=1, total_pages=2)
    page2 = _obs_page([_obs(id="b", type="TOOL", name="y")], page=2, total_pages=2)
    mock_client.api.legacy.observations_v1.get_many.side_effect = [page1, page2]
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])

    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await reader.list_spans_in_window(now, now, filter=MonitoringFilter(tags=["run:7"]))

    assert {i.id for i in items} == {"a", "b"}
    # tag filter is sent server-side via the traceTags advanced filter
    first_call = mock_client.api.legacy.observations_v1.get_many.call_args_list[0]
    sent = json.loads(first_call.kwargs["filter"])
    assert sent[0]["column"] == "traceTags"
    assert sent[0]["value"] == ["run:7"]
    # every read is scoped to the active source via environment
    assert first_call.kwargs["environment"] == "tai"
    # page-based pagination followed
    assert mock_client.api.legacy.observations_v1.get_many.call_count == 2


def _trace(**kw):
    base = {
        "id": "t1",
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "tags": ["run:7"],
        "output": {"r": 1},
        "observations": [],
    }
    base.update(kw)
    return SimpleNamespace(model_dump=lambda: base)


async def test_get_trace_maps_to_neutral(manager, mock_client):
    obs = {
        "id": "o1",
        "type": "TOOL",
        "name": "search",
        "input": {"q": 1},
        "output": {"a": 2},
        "parent_observation_id": "p",
        "usage_details": {"input": 5},
        "start_time": datetime(2026, 1, 1, tzinfo=UTC),
    }
    mock_client.api.trace.get.return_value = _trace(observations=[obs])

    t = await LangfuseReader(manager).get_trace("t1")
    assert t.id == "t1"
    assert t.tags == ["run:7"]
    assert t.output == {"r": 1}
    assert len(t.observations) == 1
    o = t.observations[0]
    assert o.name == "search"
    assert o.type == "TOOL"
    assert o.parent_id == "p"
    assert o.usage == {"input": 5}
    assert o.input == {"q": 1}
    assert o.output == {"a": 2}


async def test_map_preserves_present_falsy_values(manager, mock_client):
    # A present 0.0 cost / empty-string model must survive mapping (not collapse
    # to None via an `or` fallback).
    obs = {"id": "o1", "type": "TOOL", "model": "", "usage_details": {}, "start_time": None}
    mock_client.api.trace.get.return_value = _trace(total_cost=0.0, observations=[obs])
    t = await LangfuseReader(manager).get_trace("t1")
    assert t.total_cost == 0.0
    assert t.observations[0].model == ""


async def test_get_trace_not_found_raises(manager, mock_client):
    mock_client.api.trace.get.side_effect = NotFoundError(body="missing")
    with pytest.raises(TraceNotFoundError):
        await LangfuseReader(manager).get_trace("missing")


async def test_get_trace_transient_error_propagates(manager, mock_client):
    # A timeout is backend health, not "not found": it must surface to the
    # caller (who may retry), never map to TraceNotFoundError or an empty trace.
    mock_client.api.trace.get.side_effect = TimeoutError("slow")
    with pytest.raises(TimeoutError, match="slow"):
        await LangfuseReader(manager).get_trace("t")


def _trace_row(**kw):
    """A trace.list summary row (``TraceWithDetails``), snake_case as the SDK model
    exposes it: carries the io + metrics field groups, no observation bodies."""
    base = {
        "id": "t1",
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "name": None,
        "tags": [],
        "metadata": None,
        "input": None,
        "output": None,
        "latency": None,
        "total_cost": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _list_returns(mock_client, rows, *, total_pages=1):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=rows, meta=SimpleNamespace(total_pages=total_pages))


def _route_metrics(mock_client, *, ranking=None, tokens=None):
    """Route ``metrics_v1.metrics``: the ranking query (carries ``orderBy``) versus
    the token join query (``totalTokens``, no ``orderBy``)."""
    ranking = ranking if ranking is not None else []
    tokens = tokens if tokens is not None else {}

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=list(ranking))
        return SimpleNamespace(data=[{"id": k, "sum_totalTokens": v} for k, v in tokens.items()])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call


def _errors_for(mock_client, trace_ids=(), *, total_pages=1):
    obs = [_obs(id=f"err-{i}", trace_id=tid, level="ERROR") for i, tid in enumerate(trace_ids)]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(obs, total_pages=total_pages)


async def test_list_traces_summarizes_without_bodies(manager, mock_client):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _list_returns(
        mock_client,
        [
            _trace_row(id="a", latency=1.5, total_cost=0.25, input="hi", output={"r": 1}, tags=["run:7"]),
            _trace_row(id="b", latency=None, total_cost=0.0),
        ],
    )
    _route_metrics(mock_client, tokens={"a": 42})
    _errors_for(mock_client, ["b"])

    result = await LangfuseReader(manager).list_traces(
        from_timestamp=now, limit=5, page=2, filter=MonitoringFilter(tags=["x"])
    )
    assert [t.id for t in result] == ["a", "b"]
    a, b = result
    assert a.latency_ms == 1500.0
    assert a.total_cost == 0.25
    assert a.total_tokens == 42
    assert a.input_preview == "hi"
    assert a.output_preview == {"r": 1}
    assert a.tags == ["run:7"]
    assert a.status == "ok"
    # b carries an error observation and no usage row.
    assert b.status == "error"
    assert b.total_tokens is None
    assert b.latency_ms is None

    # A trace body is NEVER fetched while listing — get_trace is the only body door.
    mock_client.api.trace.get.assert_not_called()

    # The bounded-history guard lives or dies on from_timestamp reaching
    # trace.list — an unbounded scan is what times out. Assert the forwarding.
    list_kwargs = mock_client.api.trace.list.call_args.kwargs
    assert list_kwargs["from_timestamp"] == now
    assert list_kwargs["tags"] == ["x"]
    assert list_kwargs["limit"] == 5
    assert list_kwargs["page"] == 2
    assert list_kwargs["order_by"] == "timestamp.desc"
    assert list_kwargs["fields"] == "core,io,metrics"
    # every read is scoped to the active source via environment
    assert list_kwargs["environment"] == "tai"


async def test_list_traces_token_join_absent_is_none_not_zero(manager, mock_client):
    _list_returns(mock_client, [_trace_row(id="a"), _trace_row(id="b")])
    # only "a" has a usage row; "b" is absent from the metrics result.
    _route_metrics(mock_client, tokens={"a": 7})
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces()
    by_id = {t.id: t for t in result}
    assert by_id["a"].total_tokens == 7
    assert by_id["b"].total_tokens is None


async def test_list_traces_page_is_at_most_three_calls(manager, mock_client):
    _list_returns(mock_client, [_trace_row(id="a"), _trace_row(id="b")])
    _route_metrics(mock_client, tokens={"a": 1, "b": 2})
    _errors_for(mock_client, [])
    await LangfuseReader(manager).list_traces()
    # One list call, one token metrics call, one (single-page) observations call.
    assert mock_client.api.trace.list.call_count == 1
    assert mock_client.api.legacy.metrics_v1.metrics.call_count == 1
    assert mock_client.api.legacy.observations_v1.get_many.call_count == 1
    mock_client.api.trace.get.assert_not_called()


async def test_list_traces_error_status_drains_all_observation_pages(manager, mock_client):
    _list_returns(mock_client, [_trace_row(id="a"), _trace_row(id="b")])
    _route_metrics(mock_client, tokens={})
    page1 = _obs_page([_obs(id="e1", trace_id="a", level="ERROR")], page=1, total_pages=2)
    page2 = _obs_page([_obs(id="e2", trace_id="b", level="ERROR")], page=2, total_pages=2)
    mock_client.api.legacy.observations_v1.get_many.side_effect = [page1, page2]
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.status for t in result} == {"a": "error", "b": "error"}
    assert mock_client.api.legacy.observations_v1.get_many.call_count == 2


async def test_list_traces_token_truncation_raises(manager, mock_client):
    _list_returns(mock_client, [_trace_row(id="uncovered")])
    # The token query hits the row cap yet the page id is absent from the result:
    # the join cannot tell "no usage" from "truncated" — it must raise, not None.
    from tai42_monitoring_langfuse.reader import _METRIC_ROW_LIMIT_MAX

    def _call(query, request_options=None):
        rows = [{"id": f"other-{i}", "sum_totalTokens": 1} for i in range(_METRIC_ROW_LIMIT_MAX)]
        return SimpleNamespace(data=rows)

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()


async def test_list_traces_token_cap_null_sum_page_id_is_covered(manager, mock_client):
    # The token query hits the row cap, and the page id IS present with a null sum:
    # a null sum means no usage, so the id is covered — no truncation raise, tokens None.
    _list_returns(mock_client, [_trace_row(id="p")])
    from tai42_monitoring_langfuse.reader import _METRIC_ROW_LIMIT_MAX

    def _call(query, request_options=None):
        rows = [{"id": f"other-{i}", "sum_totalTokens": 1} for i in range(_METRIC_ROW_LIMIT_MAX - 1)]
        rows.append({"id": "p", "sum_totalTokens": None})
        return SimpleNamespace(data=rows)

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result} == {"p": None}


async def test_list_traces_token_measure_unrecognised_raises(manager, mock_client):
    # Rows come back but none carry a totalTokens-like measure key: the query
    # shape is wrong — raise rather than report every trace as usage-less.
    _list_returns(mock_client, [_trace_row(id="a")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "someOtherColumn": 5}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()


async def test_list_traces_token_measure_null_is_none_not_raise(manager, mock_client):
    # A page of tool runs with no LLM usage: the summed measure comes back null on
    # every row. The column IS present, so the query shape is right — tokens are
    # simply None, never a wrong-shape raise.
    _list_returns(mock_client, [_trace_row(id="a"), _trace_row(id="b")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": None}, {"id": "b", "sum_totalTokens": None}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result} == {"a": None, "b": None}


async def test_list_traces_token_measure_numeric_string_parses(manager, mock_client):
    # ClickHouse-backed sums serialise as strings; a "17" measure is 17 tokens.
    _list_returns(mock_client, [_trace_row(id="a")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": "17"}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result}["a"] == 17


async def test_list_traces_token_measure_non_finite_is_none(manager, mock_client):
    # A non-finite sum ("nan"/"inf") is not a token count — the column is present
    # (no wrong-shape raise), the value is simply no usage.
    _list_returns(mock_client, [_trace_row(id="a")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": "nan"}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result} == {"a": None}


async def test_list_traces_token_query_carries_page_filter(manager, mock_client):
    # The token metrics population is scoped by the page's own filter, so a cap
    # truncation reflects the filtered rows, not the whole window.
    _list_returns(mock_client, [_trace_row(id="a", tags=["run:7"])])
    captured: dict = {}

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        captured["filters"] = q["filters"]
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": 5}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    _errors_for(mock_client, [])
    await LangfuseReader(manager).list_traces(filter=MonitoringFilter(tags=["run:7"]))
    assert {"column": "tags", "operator": "any of", "value": ["run:7"], "type": "arrayOptions"} in captured["filters"]


async def test_list_traces_error_window_reaches_now_for_in_flight_row(manager, mock_client):
    # A row with no latency is in flight; the error window's upper bound must reach
    # the present so a late error observation is still caught.
    now = datetime.now(UTC)
    _list_returns(mock_client, [_trace_row(id="a", timestamp=now - timedelta(hours=1), latency=None)])
    _route_metrics(mock_client, tokens={"a": 1})
    _errors_for(mock_client, [])
    await LangfuseReader(manager).list_traces()
    to_start = mock_client.api.legacy.observations_v1.get_many.call_args.kwargs["to_start_time"]
    assert to_start >= now


async def test_list_traces_error_window_covers_long_latency(manager, mock_client):
    # A completed row's error window extends past its estimated end
    # (timestamp + latency), not just its timestamp.
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    _list_returns(mock_client, [_trace_row(id="a", timestamp=ts, latency=3600.0)])
    _route_metrics(mock_client, tokens={"a": 1})
    _errors_for(mock_client, [])
    await LangfuseReader(manager).list_traces()
    to_start = mock_client.api.legacy.observations_v1.get_many.call_args.kwargs["to_start_time"]
    assert to_start >= ts + timedelta(seconds=3600)


async def test_list_traces_error_pages_exceed_budget_raises(manager, mock_client):
    # The error walk is bounded: pages reported past the budget stop with a loud
    # error, never an unbounded scan.
    from tai42_monitoring_langfuse.reader import _ERROR_PAGE_BUDGET

    _list_returns(mock_client, [_trace_row(id="a")])
    _route_metrics(mock_client, tokens={"a": 1})
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(
        [_obs(id="e", trace_id="a", level="ERROR")], total_pages=_ERROR_PAGE_BUDGET + 5
    )
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()
    assert mock_client.api.legacy.observations_v1.get_many.call_count <= _ERROR_PAGE_BUDGET


async def test_list_traces_error_full_page_without_count_raises(manager, mock_client):
    # A full page but no page count to bound the walk: raise, never stop quietly
    # on a partial error set.
    from tai42_monitoring_langfuse.reader import _PAGE_SIZE

    _list_returns(mock_client, [_trace_row(id="a")])
    _route_metrics(mock_client, tokens={"a": 1})
    full = [_obs(id=f"e{i}", trace_id="a", level="ERROR") for i in range(_PAGE_SIZE)]
    mock_client.api.legacy.observations_v1.get_many.return_value = SimpleNamespace(data=full, meta=None)
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()


# --- source / environment scoping --------------------------------------------


async def test_query_metrics_adds_environment_clause(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    caller_clause = {"column": "name", "operator": "=", "value": "flow-a", "type": "string"}
    await LangfuseReader(manager).query_metrics(
        MetricsFilter(metrics=["count"], from_timestamp=now, to_timestamp=now, filters=[caller_clause])
    )
    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    # The environment scope is ADDED to the caller's filters, not replacing them.
    assert caller_clause in query["filters"]
    assert {"column": "environment", "operator": "=", "value": "tai", "type": "string"} in query["filters"]


# --- filter mapping ------------------------------------------------------------


async def test_span_filter_maps_native_and_advanced(manager, mock_client):
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page([])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await LangfuseReader(manager).list_spans_in_window(
        now,
        now,
        filter=MonitoringFilter(
            name="node",
            user_id="u1",
            level=None,
            model="gpt",
            min_cost=0.1,
            max_cost=2.0,
            min_tokens=10,
            max_tokens=100,
            min_latency=0.5,
            max_latency=5.0,
            metadata={"k": "v"},
        ),
    )
    kwargs = mock_client.api.legacy.observations_v1.get_many.call_args.kwargs
    assert kwargs["name"] == "node"
    assert kwargs["user_id"] == "u1"
    sent = json.loads(kwargs["filter"])
    cols = {(c["column"], c["operator"]): c for c in sent}
    assert ("model", "=") in cols
    assert cols[("model", "=")]["value"] == "gpt"
    assert cols[("totalCost", ">=")]["value"] == 0.1
    assert cols[("totalCost", "<=")]["value"] == 2.0
    assert cols[("totalTokens", ">=")]["value"] == 10
    assert cols[("totalTokens", "<=")]["value"] == 100
    assert cols[("latency", ">=")]["value"] == 0.5
    assert cols[("latency", "<=")]["value"] == 5.0
    meta = cols[("metadata", "=")]
    assert meta["type"] == "stringObject"
    assert meta["key"] == "k"
    assert meta["value"] == "v"


async def test_trace_filter_level_and_metrics_to_advanced(manager, mock_client):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(
        filter=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=1.0, metadata={"a": "b"})
    )
    kwargs = mock_client.api.trace.list.call_args.kwargs
    sent = json.loads(kwargs["filter"])
    cols = {(c["column"], c["operator"]): c for c in sent}
    assert cols[("level", "=")]["value"] == "ERROR"
    assert cols[("totalCost", ">=")]["value"] == 1.0
    assert cols[("metadata", "=")]["key"] == "a"


async def test_trace_advanced_filter_folds_time_bounds_into_json(manager, mock_client):
    # An advanced clause forces the filter JSON, which overrides the native
    # from/to params — the time bounds must ride inside the JSON too.
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)
    await LangfuseReader(manager).list_traces(from_timestamp=t0, to_timestamp=t1, filter=MonitoringFilter(min_cost=1.0))
    sent = json.loads(mock_client.api.trace.list.call_args.kwargs["filter"])
    ts = [c for c in sent if c["column"] == "timestamp"]
    assert {"column": "timestamp", "operator": ">=", "value": t0.isoformat(), "type": "datetime"} in ts
    # Half-open upper bound: exclusive.
    assert {"column": "timestamp", "operator": "<", "value": t1.isoformat(), "type": "datetime"} in ts


async def test_trace_filter_model_raises(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(filter=MonitoringFilter(model="gpt"))


async def test_span_session_id_resolves_and_postfilters(manager, mock_client):
    observations = [
        _obs(id="keep", trace_id="t-keep", type="TOOL", name="x"),
        _obs(id="drop", trace_id="t-other", type="TOOL", name="y"),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="t-keep")], meta=SimpleNamespace(total_pages=1)
    )
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await LangfuseReader(manager).list_spans_in_window(now, now, filter=MonitoringFilter(session_id="s1"))
    assert {i.id for i in items} == {"keep"}
    assert mock_client.api.trace.list.call_args.kwargs["session_id"] == "s1"
    assert mock_client.api.trace.list.call_args.kwargs["environment"] == "tai"


async def test_span_session_resolution_drains_all_pages(manager, mock_client):
    observations = [
        _obs(id="p1", trace_id="t-page1", type="TOOL", name="x"),
        _obs(id="p2", trace_id="t-page2", type="TOOL", name="y"),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.list.side_effect = [
        SimpleNamespace(data=[SimpleNamespace(id="t-page1")], meta=SimpleNamespace(total_pages=2)),
        SimpleNamespace(data=[SimpleNamespace(id="t-page2")], meta=SimpleNamespace(total_pages=2)),
    ]
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await LangfuseReader(manager).list_spans_in_window(now, now, filter=MonitoringFilter(session_id="s1"))
    # Both pages of the session's trace ids were collected before filtering.
    assert {i.id for i in items} == {"p1", "p2"}
    assert mock_client.api.trace.list.call_count == 2


# --- sorting -------------------------------------------------------------------


async def test_span_default_sort_is_newest_first(manager, mock_client):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        _obs(id="old", type="TOOL", name="a", start_time=t.replace(hour=1)),
        _obs(id="new", type="TOOL", name="b", start_time=t.replace(hour=9)),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    items = await LangfuseReader(manager).list_spans_in_window(t, t)
    assert [i.id for i in items] == ["new", "old"]


async def test_span_sort_by_duration_open_span_last(manager, mock_client):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        _obs(id="short", type="TOOL", name="a", start_time=t, end_time=t.replace(minute=1)),
        _obs(id="long", type="TOOL", name="b", start_time=t, end_time=t.replace(minute=5)),
        _obs(id="open", type="TOOL", name="c", start_time=t, end_time=None),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    items = await LangfuseReader(manager).list_spans_in_window(
        t, t, order_by=OrderBy(field="duration", direction="desc")
    )
    assert [i.id for i in items] == ["long", "short", "open"]


async def test_span_sort_by_name_asc(manager, mock_client):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        _obs(id="b", type="TOOL", name="beta", start_time=t),
        _obs(id="a", type="TOOL", name="alpha", start_time=t),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    items = await LangfuseReader(manager).list_spans_in_window(t, t, order_by=OrderBy(field="name", direction="asc"))
    assert [i.id for i in items] == ["a", "b"]


async def test_span_sort_unknown_field_raises(manager, mock_client):
    mock_client.api.legacy.observations_v1.get_many.return_value = _obs_page([])
    t = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_spans_in_window(t, t, order_by=OrderBy(field="tags"))


_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _metric_query(mock_client):
    """The most recent ranking metrics query (the one carrying ``orderBy``) —
    distinct from the token-join query issued on the same metrics endpoint."""
    for call in reversed(mock_client.api.legacy.metrics_v1.metrics.call_args_list):
        q = json.loads(call.kwargs["query"])
        if "orderBy" in q:
            return q
    raise AssertionError("no ranking metrics query was issued")


async def test_trace_native_sort_forwarded(manager, mock_client):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(order_by=OrderBy(field="name", direction="asc"))
    assert mock_client.api.trace.list.call_args.kwargs["order_by"] == "name.asc"
    # native sort never touches the metrics endpoint (empty page -> no enrichment)
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()


async def test_native_sort_preserves_trace_list_order(manager, mock_client):
    # Order guard: native path returns trace.list order untouched.
    _list_returns(mock_client, [_trace_row(id="c"), _trace_row(id="a"), _trace_row(id="b")])
    _route_metrics(mock_client, tokens={})
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces(order_by=OrderBy(field="name"))
    assert [t.id for t in result] == ["c", "a", "b"]


async def test_native_sort_allows_none_from_timestamp(manager, mock_client):
    # Native path input validation is unchanged: from_timestamp=None is forwarded.
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(from_timestamp=None, limit=5)
    assert mock_client.api.trace.list.call_args.kwargs["from_timestamp"] is None


@pytest.mark.parametrize(
    ("field", "measure"),
    [("total_cost", "totalCost"), ("latency", "latency"), ("total_tokens", "totalTokens")],
)
async def test_metric_sort_builds_query(manager, mock_client, field, measure):
    _route_metrics(mock_client, ranking=[{"id": "a", f"sum_{measure}": 5}], tokens={})
    _list_returns(mock_client, [_trace_row(id="a")])
    _errors_for(mock_client, [])
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field=field, direction="desc"), from_timestamp=_NOW, limit=5
    )
    q = _metric_query(mock_client)
    assert q["view"] == "traces"
    assert q["dimensions"] == [{"field": "id"}]
    # sum (NOT avg, even for latency) so the orderBy sum_<measure> key matches.
    assert q["metrics"] == [{"measure": measure, "aggregation": "sum"}]
    assert q["orderBy"] == [{"field": f"sum_{measure}", "direction": "desc"}]
    assert q["fromTimestamp"] == _NOW.isoformat()
    assert q["toTimestamp"]
    assert q["config"]["row_limit"] == 5
    # metric sort resolves the ranked ids' rows via trace.list, never trace.get.
    mock_client.api.trace.list.assert_called()
    mock_client.api.trace.get.assert_not_called()


async def test_metric_sort_direction_asc(manager, mock_client):
    _route_metrics(mock_client, ranking=[], tokens={})
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="asc"), from_timestamp=_NOW, limit=5
    )
    assert _metric_query(mock_client)["orderBy"][0]["direction"] == "asc"


async def test_metric_sort_filters_exact_clauses(manager, mock_client):
    _route_metrics(mock_client, ranking=[], tokens={})
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"),
        from_timestamp=_NOW,
        limit=5,
        filter=MonitoringFilter(name="flow-a", user_id="u1", session_id="s1", tags=["run:7"], metadata={"k": "v"}),
    )
    filters = _metric_query(mock_client)["filters"]
    # tags rides the metrics column `tags` (arrayOptions), never `traceTags`.
    assert {"column": "tags", "operator": "any of", "value": ["run:7"], "type": "arrayOptions"} in filters
    assert not any(c["column"] == "traceTags" for c in filters)
    cols = {c["column"]: c for c in filters}
    assert cols["name"]["value"] == "flow-a"
    assert cols["userId"]["value"] == "u1"
    assert cols["sessionId"]["value"] == "s1"
    assert cols["environment"]["value"] == "tai"
    assert cols["metadata"]["type"] == "stringObject"
    assert cols["metadata"]["key"] == "k"
    assert cols["metadata"]["value"] == "v"


async def test_metric_sort_preserves_ranked_order(manager, mock_client):
    # Rows in non-alphabetical rank order; the resolved rows' cost deliberately
    # disagrees with the rank, so any client re-sort would break the order.
    _route_metrics(
        mock_client,
        ranking=[
            {"id": "c", "sum_totalCost": 9},
            {"id": "a", "sum_totalCost": 5},
            {"id": "b", "sum_totalCost": 1},
        ],
        tokens={},
    )
    _list_returns(
        mock_client,
        [_trace_row(id="c", total_cost=1.0), _trace_row(id="a", total_cost=9.0), _trace_row(id="b", total_cost=5.0)],
    )
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="desc"), from_timestamp=_NOW, limit=5
    )
    assert [t.id for t in result] == ["c", "a", "b"]
    # the resolved row's cost survives, distinct from the rank
    assert result[0].total_cost == 1.0


async def test_metric_sort_page_slice(manager, mock_client):
    ranking = [{"id": f"r{i}", "sum_totalCost": 10 - i} for i in range(10)]
    _route_metrics(mock_client, ranking=ranking, tokens={})
    _list_returns(mock_client, [_trace_row(id=f"r{i}") for i in range(10)])
    _errors_for(mock_client, [])
    r = LangfuseReader(manager)
    res = await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2)
    assert [t.id for t in res] == ["r3", "r4", "r5"]
    assert _metric_query(mock_client)["config"]["row_limit"] == 6
    res = await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=1)
    assert [t.id for t in res] == ["r0", "r1", "r2"]
    assert _metric_query(mock_client)["config"]["row_limit"] == 3
    res = await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=None)
    assert [t.id for t in res] == ["r0", "r1", "r2"]


async def test_metric_sort_page_past_end_is_empty(manager, mock_client):
    ranking = [
        {"id": "r0", "sum_totalCost": 3},
        {"id": "r1", "sum_totalCost": 2},
        {"id": "r2", "sum_totalCost": 1},
    ]
    _route_metrics(mock_client, ranking=ranking, tokens={})
    res = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2
    )
    assert res == []
    # An empty ranked slice never walks trace.list.
    mock_client.api.trace.list.assert_not_called()


async def test_metric_sort_partial_last_page(manager, mock_client):
    ranking = [{"id": f"r{i}", "sum_totalCost": 4 - i} for i in range(4)]
    _route_metrics(mock_client, ranking=ranking, tokens={})
    _list_returns(mock_client, [_trace_row(id=f"r{i}") for i in range(4)])
    _errors_for(mock_client, [])
    res = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2
    )
    assert [t.id for t in res] == ["r3"]


async def test_metric_sort_row_limit_cap_inclusive(manager, mock_client):
    _route_metrics(mock_client, ranking=[], tokens={})
    r = LangfuseReader(manager)
    await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=1000, page=1)
    assert _metric_query(mock_client)["config"]["row_limit"] == 1000
    with pytest.raises(MonitoringReadNotSupportedError):
        await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=600, page=2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": None},
        {"limit": 0},
        {"page": 0, "limit": 5},
        {"from_timestamp": None, "limit": 5},
    ],
)
async def test_metric_sort_input_validation_raises_before_fetch(manager, mock_client, kwargs):
    base = {"order_by": OrderBy(field="total_cost"), "from_timestamp": _NOW}
    base.update(kwargs)
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(**base)
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()
    mock_client.api.trace.list.assert_not_called()


@pytest.mark.parametrize(
    "flt",
    [
        MonitoringFilter(level=MonitoringLevel.ERROR),
        MonitoringFilter(model="gpt"),
        MonitoringFilter(min_cost=1.0),
        MonitoringFilter(max_cost=1.0),
        MonitoringFilter(min_tokens=1),
        MonitoringFilter(max_tokens=1),
        MonitoringFilter(min_latency=1.0),
        MonitoringFilter(max_latency=1.0),
    ],
)
async def test_metric_sort_unsupported_filter_raises_before_fetch(manager, mock_client, flt):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(
            order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=5, filter=flt
        )
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()
    mock_client.api.trace.list.assert_not_called()


async def test_metric_sort_unsupported_filter_names_clauses(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError) as exc:
        await LangfuseReader(manager).list_traces(
            order_by=OrderBy(field="total_cost"),
            from_timestamp=_NOW,
            limit=5,
            filter=MonitoringFilter(min_cost=1.0, max_tokens=10),
        )
    assert "min_cost" in str(exc.value)
    assert "max_tokens" in str(exc.value)


async def test_metric_sort_filter_ok_on_default_sort(manager, mock_client):
    # The same level/range filter that a metric sort rejects works on the
    # default (native) sort via trace.list.
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(filter=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=1.0))
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()


def _token_query(mock_client):
    """The token-join metrics query (the one with no ``orderBy``)."""
    for call in reversed(mock_client.api.legacy.metrics_v1.metrics.call_args_list):
        q = json.loads(call.kwargs["query"])
        if "orderBy" not in q:
            return q
    raise AssertionError("no token-join metrics query was issued")


async def test_native_sort_token_query_drops_unsupported_filter(manager, mock_client):
    # The token join populates a native (default) sort page. A level/cost filter is
    # legal on that sort (trace.list honours it), so the token query must narrow its
    # population by the view-supported clauses and DROP the unsupported ones rather
    # than raise — correctness is the per-id join, not this filter.
    _list_returns(mock_client, [_trace_row(id="a")])
    _route_metrics(mock_client, tokens={"a": 5})
    _errors_for(mock_client, [])
    result = await LangfuseReader(manager).list_traces(
        filter=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=1.0, min_tokens=10, max_latency=100.0)
    )
    assert [t.id for t in result] == ["a"]
    filters = _token_query(mock_client)["filters"]
    columns = {c["column"] for c in filters}
    assert "level" not in columns
    assert "totalCost" not in columns
    assert "totalTokens" not in columns
    assert "latency" not in columns


async def test_native_sort_token_query_keeps_supported_filter(manager, mock_client):
    # The view-supported clauses (name / tags) DO ride the token population query.
    _list_returns(mock_client, [_trace_row(id="a", tags=["run:7"])])
    _route_metrics(mock_client, tokens={"a": 5})
    _errors_for(mock_client, [])
    await LangfuseReader(manager).list_traces(filter=MonitoringFilter(name="flow-a", tags=["run:7"]))
    filters = _token_query(mock_client)["filters"]
    cols = {c["column"]: c for c in filters}
    assert cols["name"]["value"] == "flow-a"
    assert cols["tags"] == {"column": "tags", "operator": "any of", "value": ["run:7"], "type": "arrayOptions"}


async def test_metric_sort_ranked_id_absent_from_window_raises(manager, mock_client):
    # METRIC path coverage: a ranked id the list window never surfaces cannot be
    # resolved into a row — the page raises loudly rather than returning short.
    _route_metrics(mock_client, ranking=[{"id": "ghost", "sum_totalCost": 9}], tokens={})
    _list_returns(mock_client, [])  # a single short page ends the walk
    _errors_for(mock_client, [])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=5)
    mock_client.api.trace.get.assert_not_called()


async def test_metric_sort_walk_page_budget_raises(manager, mock_client):
    # The walk is bounded: full pages that never carry the ranked id stop at the
    # page budget with a loud error, never an unbounded scan.
    from tai42_monitoring_langfuse.reader import _METRIC_LIST_PAGE_BUDGET, _PAGE_SIZE

    _route_metrics(mock_client, ranking=[{"id": "ghost", "sum_totalCost": 9}], tokens={})
    full = [_trace_row(id=f"x{i}") for i in range(_PAGE_SIZE)]
    mock_client.api.trace.list.return_value = SimpleNamespace(
        data=full, meta=SimpleNamespace(total_pages=_METRIC_LIST_PAGE_BUDGET + 5)
    )
    _errors_for(mock_client, [])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=5)
    assert mock_client.api.trace.list.call_count <= _METRIC_LIST_PAGE_BUDGET + 1


async def test_trace_sort_unknown_field_raises(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="metadata"))
