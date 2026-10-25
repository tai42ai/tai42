"""``list_spans_in_window``: one item per step observation in the window, from the observations list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.monitoring import (
    MonitoringError,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringReadNotServedError,
    OrderBy,
    SpanKind,
)

from tai42_monitoring_langfuse.observation_query import EXPAND_METADATA

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def _steps(fake):
    fake.add(id="root", trace_id="t1", start=T0, end=T0 + timedelta(seconds=9), tags=["a"], name="run")
    fake.add(
        id="tool",
        trace_id="t1",
        parent_id="root",
        type="TOOL",
        name="echo",
        start=T0 + timedelta(seconds=1),
        end=T0 + timedelta(seconds=2),
        tags=["a"],
        input='{"x": 1}',
        output="ok",
        metadata={"attributes.tai42.metadata": '{"k": "' + "v" * 300 + '"}'},
    )
    fake.add(
        id="node",
        trace_id="t1",
        parent_id="root",
        type="CHAIN",
        name="plan",
        start=T0 + timedelta(seconds=3),
        end=T0 + timedelta(seconds=7),
    )
    fake.add(
        id="group",
        trace_id="t1",
        parent_id="root",
        type="SPAN",
        start=T0 + timedelta(seconds=4),
        metadata={"attributes.tai42.step_role": "grouping"},
    )
    fake.add(
        id="sub",
        trace_id="t1",
        parent_id="tool",
        type="TOOL",
        start=T0 + timedelta(seconds=5),
        metadata={"attributes.tai42.step_role": "sub_step"},
    )
    fake.add(id="gen", trace_id="t1", parent_id="node", type="GENERATION", start=T0 + timedelta(seconds=6))
    fake.add(id="evt", trace_id="t1", parent_id="node", type="EVENT", start=T0 + timedelta(seconds=6))
    fake.add(id="other-run", trace_id="t2", type="TOOL", start=T0 + timedelta(seconds=8), name="echo")
    fake.add(id="later", trace_id="t3", type="TOOL", start=T1)


async def test_one_item_per_step_newest_first(fake, reader):
    _steps(fake)
    items = await reader.list_spans_in_window(T0, T1)
    assert [i.id for i in items] == ["other-run", "node", "tool", "root"]
    tool = items[2]
    assert tool.parent_id == "root"
    assert tool.name == "echo"
    assert tool.tags == ["a"]
    assert tool.tags_available is True
    assert tool.input == {"x": 1}
    assert tool.output == "ok"
    assert tool.metadata == {"attributes.tai42.metadata": {"k": "v" * 300}}
    assert tool.end == T0 + timedelta(seconds=2)


async def test_the_window_read_sends_one_filter(fake, reader):
    _steps(fake)
    await reader.list_spans_in_window(T0, T1, run="t1", kind=SpanKind.TOOL)
    [call] = fake.calls_of("observations")
    assert call["expandMetadata"] == EXPAND_METADATA
    assert fake.filters_sent() == [
        [
            {"column": "environment", "operator": "any of", "value": ["tai"], "type": "stringOptions"},
            {"column": "startTime", "operator": ">=", "value": T0.isoformat(), "type": "datetime"},
            {"column": "startTime", "operator": "<", "value": T1.isoformat(), "type": "datetime"},
            {"column": "type", "operator": "any of", "value": ["RETRIEVER", "TOOL"], "type": "stringOptions"},
            {"column": "traceId", "operator": "=", "value": "t1", "type": "string"},
        ]
    ]


async def test_run_and_kind_narrow_the_steps(fake, reader):
    _steps(fake)
    assert [i.id for i in await reader.list_spans_in_window(T0, T1, run="t1", kind=SpanKind.TOOL)] == ["tool"]
    assert [i.id for i in await reader.list_spans_in_window(T0, T1, kind=SpanKind.CHAIN)] == ["node", "root"]


@pytest.mark.parametrize("kind", [SpanKind.LLM, SpanKind.EVENT])
async def test_a_kind_no_step_has_reads_nothing(fake, reader, kind):
    _steps(fake)
    assert await reader.list_spans_in_window(T0, T1, kind=kind) == []
    assert fake.calls == []


async def test_filter_clauses_ride_the_filter_json(fake, reader):
    _steps(fake)
    flt = MonitoringFilter(
        name="echo",
        user_id="alice",
        session_id="s",
        version="v",
        level=MonitoringLevel.ERROR,
        model="m",
        tags=["a", "b"],
        min_cost=0.1,
        max_tokens=9,
        min_latency=1.0,
        metadata={"team": "blue"},
    )
    assert await reader.list_spans_in_window(T0, T1, filter_=flt) == []
    assert fake.filters_sent()[0][4:] == [
        {"column": "name", "operator": "=", "value": "echo", "type": "string"},
        {"column": "userId", "operator": "=", "value": "alice", "type": "string"},
        {"column": "sessionId", "operator": "=", "value": "s", "type": "string"},
        {"column": "version", "operator": "=", "value": "v", "type": "string"},
        {"column": "level", "operator": "=", "value": "ERROR", "type": "string"},
        {"column": "model", "operator": "=", "value": "m", "type": "string"},
        {"column": "traceTags", "operator": "any of", "value": ["a", "b"], "type": "arrayOptions"},
        {"column": "totalCost", "operator": ">=", "value": 0.1, "type": "number"},
        {"column": "totalTokens", "operator": "<=", "value": 9, "type": "number"},
        {"column": "latency", "operator": ">=", "value": 1.0, "type": "number"},
        {"column": "metadata", "type": "stringObject", "key": "team", "operator": "=", "value": "blue"},
    ]


async def test_a_name_filter_selects_by_the_observation_name(fake, reader):
    _steps(fake)
    items = await reader.list_spans_in_window(T0, T1, filter_=MonitoringFilter(name="echo"))
    assert [i.id for i in items] == ["other-run", "tool"]


async def test_the_window_follows_the_cursor(fake, reader):
    for n in range(1500):
        fake.add(id=f"s{n:04}", trace_id="t", type="TOOL", start=T0 + timedelta(milliseconds=n))
    items = await reader.list_spans_in_window(T0, T1)
    assert len(items) == 1500
    assert len(fake.calls_of("observations")) == 2


async def test_a_step_marker_that_does_not_decode_fails_loudly(fake, reader):
    fake.add(id="bad", trace_id="t", type="TOOL", start=T0, metadata={"attributes.tai42.metadata": "[1, 2]"})
    with pytest.raises(MonitoringError, match=r"malformed tai42\.metadata attribute on observation bad"):
        await reader.list_spans_in_window(T0, T1)


@pytest.mark.parametrize(
    ("order_by", "expected"),
    [
        (OrderBy(field="start", direction="asc"), ["root", "tool", "node", "other-run"]),
        (OrderBy(field="duration", direction="desc"), ["root", "node", "tool", "other-run"]),
        (OrderBy(field="name", direction="asc"), ["other-run", "tool", "node", "root"]),
        (OrderBy(field="id", direction="desc"), ["tool", "root", "other-run", "node"]),
    ],
)
async def test_items_sort_with_missing_keys_last(fake, reader, order_by, expected):
    _steps(fake)
    items = await reader.list_spans_in_window(T0, T1, order_by=order_by)
    assert [i.id for i in items] == expected


async def test_an_unknown_span_sort_is_not_served(fake, reader):
    _steps(fake)
    with pytest.raises(MonitoringReadNotServedError, match="cannot sort on 'cost'"):
        await reader.list_spans_in_window(T0, T1, order_by=OrderBy(field="cost"))
