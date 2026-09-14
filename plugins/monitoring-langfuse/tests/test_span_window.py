"""SpanWindowQuery: tool-granularity selection, session/run scoping, filter
mapping, and the client-side span sort for list_spans_in_window."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tai42_contract.monitoring import MonitoringFilter, MonitoringReadNotSupportedError, OrderBy, SpanKind

from tai42_monitoring_langfuse.reader import LangfuseReader


async def test_list_spans_selects_tool_granularity(manager, mock_client, obs, obs_page):
    observations = [
        obs(id="span1", type="SPAN", name="my_node"),
        obs(id="tool1", type="TOOL", name="search"),
        obs(id="gen1", type="GENERATION", name="llm"),  # nested LLM -> excluded
        obs(id="evt1", type="EVENT", name="ToolCall:search"),  # event marker -> excluded
        obs(id="grp1", type="SPAN", name="tools"),  # grouping chain -> excluded
        obs(id="jq1", type="SPAN", name="condition:check"),  # jq sub-step -> excluded
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=["run:7"])

    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await reader.list_spans_in_window(now, now)

    ids = {i.id for i in items}
    assert ids == {"span1", "tool1"}
    assert all(i.tags == ["run:7"] for i in items)


async def test_list_spans_run_scope_and_kind_narrowing(manager, mock_client, obs, obs_page):
    observations = [
        obs(id="tool1", type="TOOL", name="search"),
        obs(id="agent1", type="AGENT", name="planner"),
        obs(id="chain1", type="CHAIN", name="step"),
        obs(id="retr1", type="RETRIEVER", name="lookup"),
        obs(id="span1", type="SPAN", name="node"),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
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


async def test_list_spans_failed_tag_fetch_degrades_to_empty(manager, mock_client, obs, obs_page):
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page([obs(id="a", type="TOOL", name="x")])
    mock_client.api.trace.get.side_effect = RuntimeError("boom")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await LangfuseReader(manager).list_spans_in_window(now, now)
    assert [i.id for i in items] == ["a"]
    assert items[0].tags == []


async def test_list_spans_tag_filter_and_pagination(manager, mock_client, obs, obs_page):
    page1 = obs_page([obs(id="a", type="TOOL", name="x")], page=1, total_pages=2)
    page2 = obs_page([obs(id="b", type="TOOL", name="y")], page=2, total_pages=2)
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


async def test_span_filter_maps_native_and_advanced(manager, mock_client, obs_page):
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page([])
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


async def test_span_session_id_resolves_and_postfilters(manager, mock_client, obs, obs_page):
    observations = [
        obs(id="keep", trace_id="t-keep", type="TOOL", name="x"),
        obs(id="drop", trace_id="t-other", type="TOOL", name="y"),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
    mock_client.api.trace.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="t-keep")], meta=SimpleNamespace(total_pages=1)
    )
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = await LangfuseReader(manager).list_spans_in_window(now, now, filter=MonitoringFilter(session_id="s1"))
    assert {i.id for i in items} == {"keep"}
    assert mock_client.api.trace.list.call_args.kwargs["session_id"] == "s1"
    assert mock_client.api.trace.list.call_args.kwargs["environment"] == "tai"


async def test_span_session_resolution_drains_all_pages(manager, mock_client, obs, obs_page):
    observations = [
        obs(id="p1", trace_id="t-page1", type="TOOL", name="x"),
        obs(id="p2", trace_id="t-page2", type="TOOL", name="y"),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
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


async def test_span_default_sort_is_newest_first(manager, mock_client, obs, obs_page):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        obs(id="old", type="TOOL", name="a", start_time=t.replace(hour=1)),
        obs(id="new", type="TOOL", name="b", start_time=t.replace(hour=9)),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    items = await LangfuseReader(manager).list_spans_in_window(t, t)
    assert [i.id for i in items] == ["new", "old"]


async def test_span_sort_by_duration_open_span_last(manager, mock_client, obs, obs_page):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        obs(id="short", type="TOOL", name="a", start_time=t, end_time=t.replace(minute=1)),
        obs(id="long", type="TOOL", name="b", start_time=t, end_time=t.replace(minute=5)),
        obs(id="open", type="TOOL", name="c", start_time=t, end_time=None),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    items = await LangfuseReader(manager).list_spans_in_window(
        t, t, order_by=OrderBy(field="duration", direction="desc")
    )
    assert [i.id for i in items] == ["long", "short", "open"]


async def test_span_sort_by_name_asc(manager, mock_client, obs, obs_page):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        obs(id="b", type="TOOL", name="beta", start_time=t),
        obs(id="a", type="TOOL", name="alpha", start_time=t),
    ]
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page(observations)
    mock_client.api.trace.get.return_value = SimpleNamespace(tags=[])
    items = await LangfuseReader(manager).list_spans_in_window(t, t, order_by=OrderBy(field="name", direction="asc"))
    assert [i.id for i in items] == ["a", "b"]


async def test_span_sort_unknown_field_raises(manager, mock_client, obs_page):
    mock_client.api.legacy.observations_v1.get_many.return_value = obs_page([])
    t = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_spans_in_window(t, t, order_by=OrderBy(field="tags"))
