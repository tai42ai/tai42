"""TraceQuery enrichment: the per-trace token join and the error-status window for
list_traces summaries (truncation guards, measure parsing, window bounds)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.monitoring import MonitoringFilter, MonitoringLevel, MonitoringReadNotSupportedError

from tai42_monitoring_langfuse.reader import LangfuseReader


async def test_list_traces_token_join_absent_is_none_not_zero(
    manager, mock_client, list_returns, route_metrics, errors_for, trace_row
):
    list_returns([trace_row(id="a"), trace_row(id="b")])
    # only "a" has a usage row; "b" is absent from the metrics result.
    route_metrics(tokens={"a": 7})
    errors_for([])
    result = await LangfuseReader(manager).list_traces()
    by_id = {t.id: t for t in result}
    assert by_id["a"].total_tokens == 7
    assert by_id["b"].total_tokens is None


async def test_list_traces_error_status_drains_all_observation_pages(
    manager, mock_client, list_returns, route_metrics, obs, obs_page, trace_row
):
    list_returns([trace_row(id="a"), trace_row(id="b")])
    route_metrics(tokens={})
    page1 = obs_page([obs(id="e1", trace_id="a", level="ERROR")], page=1, total_pages=2)
    page2 = obs_page([obs(id="e2", trace_id="b", level="ERROR")], page=2, total_pages=2)
    mock_client.api.legacy.observations_v1.get_many.side_effect = [page1, page2]
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.status for t in result} == {"a": "error", "b": "error"}
    assert mock_client.api.legacy.observations_v1.get_many.call_count == 2


async def test_list_traces_token_truncation_raises(manager, mock_client, list_returns, errors_for, trace_row):
    list_returns([trace_row(id="uncovered")])
    # The token query hits the row cap yet the page id is absent from the result:
    # the join cannot tell "no usage" from "truncated" — it must raise, not None.
    from tai42_monitoring_langfuse.trace_query import _METRIC_ROW_LIMIT_MAX

    def _call(query, request_options=None):
        rows = [{"id": f"other-{i}", "sum_totalTokens": 1} for i in range(_METRIC_ROW_LIMIT_MAX)]
        return SimpleNamespace(data=rows)

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()


async def test_list_traces_token_cap_null_sum_page_id_is_covered(
    manager, mock_client, list_returns, errors_for, trace_row
):
    # The token query hits the row cap, and the page id IS present with a null sum:
    # a null sum means no usage, so the id is covered — no truncation raise, tokens None.
    list_returns([trace_row(id="p")])
    from tai42_monitoring_langfuse.trace_query import _METRIC_ROW_LIMIT_MAX

    def _call(query, request_options=None):
        rows = [{"id": f"other-{i}", "sum_totalTokens": 1} for i in range(_METRIC_ROW_LIMIT_MAX - 1)]
        rows.append({"id": "p", "sum_totalTokens": None})
        return SimpleNamespace(data=rows)

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result} == {"p": None}


async def test_list_traces_token_measure_unrecognised_raises(manager, mock_client, list_returns, errors_for, trace_row):
    # Rows come back but none carry a totalTokens-like measure key: the query
    # shape is wrong — raise rather than report every trace as usage-less.
    list_returns([trace_row(id="a")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "someOtherColumn": 5}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()


async def test_list_traces_token_measure_null_is_none_not_raise(
    manager, mock_client, list_returns, errors_for, trace_row
):
    # A page of tool runs with no LLM usage: the summed measure comes back null on
    # every row. The column IS present, so the query shape is right — tokens are
    # simply None, never a wrong-shape raise.
    list_returns([trace_row(id="a"), trace_row(id="b")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": None}, {"id": "b", "sum_totalTokens": None}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result} == {"a": None, "b": None}


async def test_list_traces_token_measure_numeric_string_parses(
    manager, mock_client, list_returns, errors_for, trace_row
):
    # ClickHouse-backed sums serialise as strings; a "17" measure is 17 tokens.
    list_returns([trace_row(id="a")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": "17"}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result}["a"] == 17


async def test_list_traces_token_measure_non_finite_is_none(manager, mock_client, list_returns, errors_for, trace_row):
    # A non-finite sum ("nan"/"inf") is not a token count — the column is present
    # (no wrong-shape raise), the value is simply no usage.
    list_returns([trace_row(id="a")])

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": "nan"}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    result = await LangfuseReader(manager).list_traces()
    assert {t.id: t.total_tokens for t in result} == {"a": None}


async def test_list_traces_token_query_carries_page_filter(manager, mock_client, list_returns, errors_for, trace_row):
    # The token metrics population is scoped by the page's own filter, so a cap
    # truncation reflects the filtered rows, not the whole window.
    list_returns([trace_row(id="a", tags=["run:7"])])
    captured: dict = {}

    def _call(query, request_options=None):
        q = json.loads(query)
        if "orderBy" in q:
            return SimpleNamespace(data=[])
        captured["filters"] = q["filters"]
        return SimpleNamespace(data=[{"id": "a", "sum_totalTokens": 5}])

    mock_client.api.legacy.metrics_v1.metrics.side_effect = _call
    errors_for([])
    await LangfuseReader(manager).list_traces(filter=MonitoringFilter(tags=["run:7"]))
    assert {"column": "tags", "operator": "any of", "value": ["run:7"], "type": "arrayOptions"} in captured["filters"]


async def test_list_traces_error_window_reaches_now_for_in_flight_row(
    manager, mock_client, list_returns, route_metrics, errors_for, trace_row
):
    # A row with no latency is in flight; the error window's upper bound must reach
    # the present so a late error observation is still caught.
    now = datetime.now(UTC)
    list_returns([trace_row(id="a", timestamp=now - timedelta(hours=1), latency=None)])
    route_metrics(tokens={"a": 1})
    errors_for([])
    await LangfuseReader(manager).list_traces()
    to_start = mock_client.api.legacy.observations_v1.get_many.call_args.kwargs["to_start_time"]
    assert to_start >= now


async def test_list_traces_error_window_covers_long_latency(
    manager, mock_client, list_returns, route_metrics, errors_for, trace_row
):
    # A completed row's error window extends past its estimated end
    # (timestamp + latency), not just its timestamp.
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    list_returns([trace_row(id="a", timestamp=ts, latency=3600.0)])
    route_metrics(tokens={"a": 1})
    errors_for([])
    await LangfuseReader(manager).list_traces()
    to_start = mock_client.api.legacy.observations_v1.get_many.call_args.kwargs["to_start_time"]
    assert to_start >= ts + timedelta(seconds=3600)


async def test_list_traces_error_pages_exceed_budget_raises(
    manager, mock_client, list_returns, route_metrics, obs, obs_page, trace_row
):
    # The error walk is bounded: pages reported past the budget stop with a loud
    # error, never an unbounded scan.
    from tai42_monitoring_langfuse.trace_query import _ERROR_PAGE_BUDGET

    list_returns([trace_row(id="a")])
    route_metrics(tokens={"a": 1})
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(
        [obs(id="e", trace_id="a", level="ERROR")], total_pages=_ERROR_PAGE_BUDGET + 5
    )
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()
    assert mock_client.api.legacy.observations_v1.get_many.call_count <= _ERROR_PAGE_BUDGET


async def test_list_traces_error_full_page_without_count_raises(
    manager, mock_client, list_returns, route_metrics, obs, trace_row
):
    # A full page but no page count to bound the walk: raise, never stop quietly
    # on a partial error set.
    from tai42_monitoring_langfuse.query_base import _PAGE_SIZE

    list_returns([trace_row(id="a")])
    route_metrics(tokens={"a": 1})
    full = [obs(id=f"e{i}", trace_id="a", level="ERROR") for i in range(_PAGE_SIZE)]
    mock_client.api.legacy.observations_v1.get_many.return_value = SimpleNamespace(data=full, meta=None)
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces()


async def test_native_sort_token_query_drops_unsupported_filter(
    manager, mock_client, list_returns, route_metrics, errors_for, token_query, trace_row
):
    # The token join populates a native (default) sort page. A level/cost filter is
    # legal on that sort (trace.list honours it), so the token query must narrow its
    # population by the view-supported clauses and DROP the unsupported ones rather
    # than raise — correctness is the per-id join, not this filter.
    list_returns([trace_row(id="a")])
    route_metrics(tokens={"a": 5})
    errors_for([])
    result = await LangfuseReader(manager).list_traces(
        filter=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=1.0, min_tokens=10, max_latency=100.0)
    )
    assert [t.id for t in result] == ["a"]
    filters = token_query()["filters"]
    columns = {c["column"] for c in filters}
    assert "level" not in columns
    assert "totalCost" not in columns
    assert "totalTokens" not in columns
    assert "latency" not in columns


async def test_native_sort_token_query_keeps_supported_filter(
    manager, mock_client, list_returns, route_metrics, errors_for, token_query, trace_row
):
    # The view-supported clauses (name / tags) DO ride the token population query.
    list_returns([trace_row(id="a", tags=["run:7"])])
    route_metrics(tokens={"a": 5})
    errors_for([])
    await LangfuseReader(manager).list_traces(filter=MonitoringFilter(name="flow-a", tags=["run:7"]))
    filters = token_query()["filters"]
    cols = {c["column"]: c for c in filters}
    assert cols["name"]["value"] == "flow-a"
    assert cols["tags"] == {"column": "tags", "operator": "any of", "value": ["run:7"], "type": "arrayOptions"}
