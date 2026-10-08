"""TraceQuery: get_trace mapping, list_traces summaries, trace filter mapping, and
the native (server-side) trace sort."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langfuse.api.commons.errors import NotFoundError
from tai42_contract.monitoring import (
    MonitoringError,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringReadNotSupportedError,
    ObservationNotFoundError,
    OrderBy,
    SpanKind,
    TraceNotFoundError,
)

from tai42_monitoring_langfuse.reader import LangfuseReader


async def test_get_trace_maps_to_neutral(manager, mock_client, trace_body):
    obs = {
        "id": "o1",
        "type": "TOOL",
        "name": "search",
        "input": {"q": 1},
        "output": {"a": 2},
        "parent_observation_id": "p",
        "usage_details": {"input": 5, "output": 3, "total": 8},
        "start_time": datetime(2026, 1, 1, tzinfo=UTC),
    }
    mock_client.api.trace.get.return_value = trace_body(observations=[obs])

    t = await LangfuseReader(manager).get_trace("t1")
    assert t.id == "t1"
    assert t.tags == ["run:7"]
    assert t.output == {"r": 1}
    assert len(t.observations) == 1
    o = t.observations[0]
    assert o.name == "search"
    assert o.kind is SpanKind.TOOL
    assert o.parent_id == "p"
    assert (o.input_tokens, o.output_tokens, o.total_tokens) == (5, 3, 8)
    assert o.input == {"q": 1}
    assert o.output == {"a": 2}


@pytest.mark.parametrize(
    ("langfuse_type", "kind"),
    [
        ("GENERATION", SpanKind.LLM),
        ("EMBEDDING", SpanKind.LLM),
        ("TOOL", SpanKind.TOOL),
        ("RETRIEVER", SpanKind.TOOL),
        ("EVENT", SpanKind.EVENT),
        ("SPAN", SpanKind.CHAIN),
        ("CHAIN", SpanKind.CHAIN),
        ("AGENT", SpanKind.CHAIN),
        ("EVALUATOR", SpanKind.CHAIN),
        ("GUARDRAIL", SpanKind.CHAIN),
        ("SOMETHING_NEW", SpanKind.CHAIN),
    ],
)
async def test_each_langfuse_type_maps_to_a_neutral_kind(manager, mock_client, trace_body, langfuse_type, kind):
    obs = {"id": "o1", "type": langfuse_type, "start_time": None}
    mock_client.api.trace.get.return_value = trace_body(observations=[obs])
    (o,) = (await LangfuseReader(manager).get_trace("t1")).observations
    assert o.kind is kind


async def test_tokens_are_read_from_the_usage_details(manager, mock_client, trace_body):
    observations = [
        {"id": "full", "type": "GENERATION", "usage_details": {"input": 7, "output": 2, "total": 9}},
        {"id": "partial", "type": "GENERATION", "usage_details": {"input": 7}},
        {"id": "none", "type": "SPAN", "usage_details": {}},
    ]
    mock_client.api.trace.get.return_value = trace_body(observations=observations)
    full, partial, none = (await LangfuseReader(manager).get_trace("t1")).observations
    assert (full.input_tokens, full.output_tokens, full.total_tokens) == (7, 2, 9)
    assert (partial.input_tokens, partial.output_tokens, partial.total_tokens) == (7, None, None)
    assert (none.input_tokens, none.output_tokens, none.total_tokens) == (None, None, None)


async def test_map_preserves_present_falsy_values(manager, mock_client, trace_body):
    # A present 0.0 cost / empty-string model must survive mapping (not collapse
    # to None via an `or` fallback).
    obs = {"id": "o1", "type": "TOOL", "model": "", "usage_details": {}, "start_time": None}
    mock_client.api.trace.get.return_value = trace_body(total_cost=0.0, observations=[obs])
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


async def test_list_traces_summarizes_without_bodies(
    manager, mock_client, trace_row, list_returns, route_metrics, errors_for
):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    list_returns(
        [
            trace_row(id="a", latency=1.5, total_cost=0.25, input="hi", output={"r": 1}, tags=["run:7"]),
            trace_row(id="b", latency=None, total_cost=0.0),
        ],
    )
    route_metrics(tokens={"a": 42})
    errors_for(["b"])

    result = await LangfuseReader(manager).list_traces(
        from_timestamp=now, limit=5, page=2, filter_=MonitoringFilter(tags=["x"])
    )
    assert [t.id for t in result] == ["a", "b"]
    a, b = result
    assert a.latency_ms == 1500.0
    assert a.total_cost == 0.25
    assert a.total_tokens == 42
    assert a.input == "hi"
    assert a.output == {"r": 1}
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


async def test_list_traces_version_filter_rides_native_param(
    manager, mock_client, trace_row, list_returns, route_metrics, errors_for
):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    list_returns([trace_row(id="a")])
    route_metrics(tokens={"a": 1})
    errors_for([])

    await LangfuseReader(manager).list_traces(from_timestamp=now, filter_=MonitoringFilter(version="5"))

    # ``version`` is a native trace.list param (not an advanced-filter clause).
    assert mock_client.api.trace.list.call_args.kwargs["version"] == "5"


async def test_list_traces_page_is_at_most_three_calls(
    manager, mock_client, trace_row, list_returns, route_metrics, errors_for
):
    list_returns([trace_row(id="a"), trace_row(id="b")])
    route_metrics(tokens={"a": 1, "b": 2})
    errors_for([])
    await LangfuseReader(manager).list_traces()
    # One list call, one token metrics call, one (single-page) observations call.
    assert mock_client.api.trace.list.call_count == 1
    assert mock_client.api.legacy.metrics_v1.metrics.call_count == 1
    assert mock_client.api.legacy.observations_v1.get_many.call_count == 1
    mock_client.api.trace.get.assert_not_called()


async def test_trace_filter_level_and_metrics_to_advanced(manager, mock_client):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(
        filter_=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=1.0, metadata={"a": "b"})
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
    await LangfuseReader(manager).list_traces(
        from_timestamp=t0, to_timestamp=t1, filter_=MonitoringFilter(min_cost=1.0)
    )
    sent = json.loads(mock_client.api.trace.list.call_args.kwargs["filter"])
    ts = [c for c in sent if c["column"] == "timestamp"]
    assert {"column": "timestamp", "operator": ">=", "value": t0.isoformat(), "type": "datetime"} in ts
    # Half-open upper bound: exclusive.
    assert {"column": "timestamp", "operator": "<", "value": t1.isoformat(), "type": "datetime"} in ts


async def test_trace_filter_model_raises(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(filter_=MonitoringFilter(model="gpt"))


async def test_trace_native_sort_forwarded(manager, mock_client):
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(order_by=OrderBy(field="name", direction="asc"))
    assert mock_client.api.trace.list.call_args.kwargs["order_by"] == "name.asc"
    # native sort never touches the metrics endpoint (empty page -> no enrichment)
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()


async def test_native_sort_preserves_trace_list_order(
    manager, mock_client, trace_row, list_returns, route_metrics, errors_for
):
    # Order guard: native path returns trace.list order untouched.
    list_returns([trace_row(id="c"), trace_row(id="a"), trace_row(id="b")])
    route_metrics(tokens={})
    errors_for([])
    result = await LangfuseReader(manager).list_traces(order_by=OrderBy(field="name"))
    assert [t.id for t in result] == ["c", "a", "b"]


async def test_native_sort_allows_none_from_timestamp(manager, mock_client):
    # Native path input validation is unchanged: from_timestamp=None is forwarded.
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(from_timestamp=None, limit=5)
    assert mock_client.api.trace.list.call_args.kwargs["from_timestamp"] is None


async def test_trace_sort_unknown_field_raises(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="metadata"))


# -- get_observation and the producer-metadata decode ---------------------------------------


def _observation_body(**kw):
    base = {"id": "o1", "trace_id": "t1", "type": "SPAN", "name": "node", "input": {"a": 1}, "output": None}
    base.update(kw)
    return SimpleNamespace(model_dump=lambda: base)


async def test_get_observation_reads_one_observation(manager, mock_client):
    mock_client.api.legacy.observations_v1.get.return_value = _observation_body()
    obs = await LangfuseReader(manager).get_observation("t1", "o1")
    assert (obs.id, obs.trace_id, obs.input) == ("o1", "t1", {"a": 1})
    assert mock_client.api.legacy.observations_v1.get.call_args.args == ("o1",)


async def test_get_observation_absent_is_not_found(manager, mock_client):
    mock_client.api.legacy.observations_v1.get.side_effect = NotFoundError(body="nope")
    with pytest.raises(ObservationNotFoundError):
        await LangfuseReader(manager).get_observation("t1", "o1")


async def test_get_observation_of_another_trace_is_not_found(manager, mock_client):
    mock_client.api.legacy.observations_v1.get.return_value = _observation_body(trace_id="other")
    with pytest.raises(ObservationNotFoundError):
        await LangfuseReader(manager).get_observation("t1", "o1")


async def test_get_observation_transient_error_propagates(manager, mock_client):
    mock_client.api.legacy.observations_v1.get.side_effect = TimeoutError("slow")
    with pytest.raises(TimeoutError):
        await LangfuseReader(manager).get_observation("t1", "o1")


@pytest.mark.parametrize(
    "encoded",
    [json.dumps({"k": "v", "tai42.message": {"content": "c"}}), {"k": "v", "tai42.message": {"content": "c"}}],
)
async def test_producer_metadata_is_decoded_with_the_promoted_keys(manager, mock_client, encoded):
    metadata = {"attributes": {"tai42.metadata": encoded, "tai42.step_role": "grouping", "other": "x"}}
    mock_client.api.legacy.observations_v1.get.return_value = _observation_body(metadata=metadata)
    obs = await LangfuseReader(manager).get_observation("t1", "o1")
    assert obs.metadata == {"k": "v", "tai42.message": {"content": "c"}, "tai42.step_role": "grouping"}


async def test_an_observation_without_producer_metadata_has_none(manager, mock_client):
    mock_client.api.legacy.observations_v1.get.return_value = _observation_body(metadata={"attributes": {"x": 1}})
    assert (await LangfuseReader(manager).get_observation("t1", "o1")).metadata is None
    mock_client.api.legacy.observations_v1.get.return_value = _observation_body(metadata=None)
    assert (await LangfuseReader(manager).get_observation("t1", "o1")).metadata is None


async def test_malformed_producer_metadata_is_refused(manager, mock_client):
    metadata = {"attributes": {"tai42.metadata": json.dumps([1, 2])}}
    mock_client.api.legacy.observations_v1.get.return_value = _observation_body(metadata=metadata)
    with pytest.raises(MonitoringError, match=r"malformed tai42\.metadata attribute on observation o1"):
        await LangfuseReader(manager).get_observation("t1", "o1")
