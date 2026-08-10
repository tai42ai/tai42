"""Reader query building, filter mapping, sorting, and hydration semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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


async def test_list_traces_lists_then_hydrates(manager, mock_client):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="a"), SimpleNamespace(id="b")])

    def _get(tid, request_options=None):
        return SimpleNamespace(model_dump=lambda: {"id": tid, "observations": []})

    mock_client.api.trace.get.side_effect = _get
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = await LangfuseReader(manager).list_traces(
        from_timestamp=now, limit=5, page=2, filter=MonitoringFilter(tags=["x"])
    )
    assert {t.id for t in result} == {"a", "b"}

    # The bounded-history guard lives or dies on from_timestamp reaching
    # trace.list — an unbounded scan is what times out. Assert the forwarding.
    list_kwargs = mock_client.api.trace.list.call_args.kwargs
    assert list_kwargs["from_timestamp"] == now
    assert list_kwargs["tags"] == ["x"]
    assert list_kwargs["limit"] == 5
    assert list_kwargs["page"] == 2
    assert list_kwargs["order_by"] == "timestamp.desc"
    # every read is scoped to the active source via environment
    assert list_kwargs["environment"] == "tai"


async def test_list_traces_marks_unfetchable(manager, mock_client):
    # NATIVE path: a body that fails to hydrate is kept in place with
    # fetch_error set, not dropped — count unchanged, order preserved.
    mock_client.api.trace.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="ok"), SimpleNamespace(id="bad")]
    )

    def _get(tid, request_options=None):
        if tid == "bad":
            raise ConnectionError("boom")
        return SimpleNamespace(model_dump=lambda: {"id": tid, "observations": []})

    mock_client.api.trace.get.side_effect = _get
    result = await LangfuseReader(manager).list_traces()
    assert [t.id for t in result] == ["ok", "bad"]
    ok, bad = result
    assert ok.fetch_error is None
    assert bad.fetch_error
    assert bad.total_cost is None
    assert bad.observations == []


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
    return json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])


def _hydrate_each(mock_client, costs=None, timestamps=None):
    """trace.get returning a body per id (snake_case, as model_dump emits)."""

    def _get(tid, request_options=None):
        body = {"id": tid, "observations": []}
        if costs is not None:
            body["total_cost"] = costs[tid]
        if timestamps is not None:
            body["timestamp"] = timestamps[tid]
        return SimpleNamespace(model_dump=lambda body=body: body)

    mock_client.api.trace.get.side_effect = _get


async def test_trace_native_sort_forwarded(manager, mock_client):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(order_by=OrderBy(field="name", direction="asc"))
    assert mock_client.api.trace.list.call_args.kwargs["order_by"] == "name.asc"
    # native sort never touches the metrics endpoint
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()


async def test_native_sort_preserves_trace_list_order(manager, mock_client):
    # Hydrate-order guard: native path returns trace.list order untouched.
    mock_client.api.trace.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="c"), SimpleNamespace(id="a"), SimpleNamespace(id="b")]
    )
    _hydrate_each(mock_client)
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
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[{"id": "a", f"sum_{measure}": 5}])
    _hydrate_each(mock_client)
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
    # metric sort never touches trace.list
    mock_client.api.trace.list.assert_not_called()


async def test_metric_sort_direction_asc(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="asc"), from_timestamp=_NOW, limit=5
    )
    assert _metric_query(mock_client)["orderBy"][0]["direction"] == "asc"


async def test_metric_sort_filters_exact_clauses(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
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
    # Rows in non-alphabetical rank order; hydrated cost and timestamp
    # deliberately disagree with the rank, so any client re-sort would break it.
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[
            {"id": "c", "sum_totalCost": 9},
            {"id": "a", "sum_totalCost": 5},
            {"id": "b", "sum_totalCost": 1},
        ]
    )
    costs = {"c": 1.0, "a": 9.0, "b": 5.0}
    timestamps = {
        "c": datetime(2026, 1, 1, tzinfo=UTC),
        "a": datetime(2026, 1, 3, tzinfo=UTC),
        "b": datetime(2026, 1, 2, tzinfo=UTC),
    }
    _hydrate_each(mock_client, costs=costs, timestamps=timestamps)
    result = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="desc"), from_timestamp=_NOW, limit=5
    )
    assert [t.id for t in result] == ["c", "a", "b"]
    # the hydrated cost survives, distinct from the rank
    assert result[0].total_cost == 1.0


async def test_metric_sort_page_slice(manager, mock_client):
    rows = [{"id": f"r{i}", "sum_totalCost": 10 - i} for i in range(10)]
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=rows)
    _hydrate_each(mock_client)
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
    rows = [
        {"id": "r0", "sum_totalCost": 3},
        {"id": "r1", "sum_totalCost": 2},
        {"id": "r2", "sum_totalCost": 1},
    ]
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=rows)
    _hydrate_each(mock_client)
    res = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2
    )
    assert res == []


async def test_metric_sort_partial_last_page(manager, mock_client):
    rows = [{"id": f"r{i}", "sum_totalCost": 4 - i} for i in range(4)]
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=rows)
    _hydrate_each(mock_client)
    res = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2
    )
    assert [t.id for t in res] == ["r3"]


async def test_metric_sort_row_limit_cap_inclusive(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
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


async def test_metric_sort_marks_unfetchable_top_in_rank(manager, mock_client):
    # METRIC path: the top-ranked id fails to hydrate -> kept at result[0] in
    # rank order with fetch_error; the neighbor is fully mapped.
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"id": "top", "sum_totalCost": 9}, {"id": "next", "sum_totalCost": 5}]
    )

    def _get(tid, request_options=None):
        if tid == "top":
            raise ConnectionError("observations too large")
        return SimpleNamespace(model_dump=lambda: {"id": tid, "total_cost": 5.0, "observations": []})

    mock_client.api.trace.get.side_effect = _get
    result = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="desc"), from_timestamp=_NOW, limit=5
    )
    assert [t.id for t in result] == ["top", "next"]
    assert result[0].fetch_error
    assert result[0].total_cost is None
    assert result[0].observations == []
    assert result[1].fetch_error is None
    assert result[1].total_cost == 5.0


async def test_trace_sort_unknown_field_raises(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="metadata"))
