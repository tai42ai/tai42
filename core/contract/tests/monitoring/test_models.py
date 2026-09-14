"""Tests for the monitoring models: the vendor-neutral trace metadata, the reader's
non-optional return contracts, and the structurally-bounded ``preview``."""

from __future__ import annotations

import inspect

import pytest

from tai42_contract.monitoring.models import MonitoringFilter


def test_set_trace_metadata_has_no_client_name():
    from tai42_contract.monitoring import Span

    params = inspect.signature(Span.set_trace_metadata).parameters
    assert "client_name" not in params
    assert {"name", "tags"} <= set(params)


def test_monitoring_get_trace_return_is_non_optional():
    # get_trace never returns None: it returns a trace or raises. The resolved
    # return hint is the bare MonitoringTrace, not MonitoringTrace | None.
    import typing

    from tai42_contract.monitoring.models import MonitoringTrace
    from tai42_contract.monitoring.reader import MonitoringReader

    hints = typing.get_type_hints(MonitoringReader.get_trace)
    assert hints["return"] is MonitoringTrace


def test_monitoring_list_traces_returns_summaries():
    # list_traces returns lightweight row summaries, never full-body traces —
    # bodies are the sole province of get_trace.
    import typing

    from tai42_contract.monitoring.models import MonitoringTraceSummary
    from tai42_contract.monitoring.reader import MonitoringReader

    hints = typing.get_type_hints(MonitoringReader.list_traces)
    assert hints["return"] == list[MonitoringTraceSummary]


def test_trace_preview_bounds_structurally():
    from datetime import datetime

    from pydantic import JsonValue

    from tai42_contract.monitoring import TRACE_PREVIEW_MAX_CHARS, preview

    # None and scalars pass through unchanged; a structured value is returned as a
    # bounded JSON value, not a JSON string.
    assert preview(None) is None
    assert preview("short") == "short"
    assert preview(42) == 42
    assert preview({"a": 1}) == {"a": 1}
    # A non-JSON leaf (datetime) falls back to str.
    assert preview(datetime(2026, 1, 1)) == "2026-01-01 00:00:00"

    # Depth: nesting past the cap is elided with a terminal marker.
    deep = {"l0": {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": "x"}}}}}}}}
    node: JsonValue = preview(deep)
    for level in ("l0", "l1", "l2", "l3", "l4", "l5"):
        assert isinstance(node, dict)
        node = node[level]
    assert node == {"…": "…"}

    # Items: an object / array past the cap keeps a "+N more" marker.
    wide_obj = preview({str(i): i for i in range(30)})
    assert isinstance(wide_obj, dict)
    assert len(wide_obj) == 21  # 20 kept + the marker key
    assert wide_obj["…"] == "+10 more"
    wide_list = preview(list(range(30)))
    assert isinstance(wide_list, list)
    assert len(wide_list) == 21
    assert wide_list[-1] == "+10 more"

    # Leaf cut: a long string leaf is clipped to the cap plus the terminal marker.
    long = "x" * (TRACE_PREVIEW_MAX_CHARS + 100)
    assert preview(long) == "x" * TRACE_PREVIEW_MAX_CHARS + "…"

    # A stringified structure is parsed and bounded as a tree.
    assert preview('{"a": 1}') == {"a": 1}
    # A stringified structure over the parse gate is NOT parsed — just char-clipped.
    big = "[" + "1," * 20_000 + "1]"
    clipped = preview(big)
    assert isinstance(clipped, str)
    assert clipped == big[:TRACE_PREVIEW_MAX_CHARS] + "…"

    # Sequence bounding slices BEFORE materializing: only the kept prefix is
    # consumed, never a full copy of the whole sequence. A list whose __iter__
    # raises proves the slice happens on the sequence, not on a full copy of it.
    class _NoFullCopy(list[int]):
        def __iter__(self) -> object:  # type: ignore[override]
            raise AssertionError("preview must slice before copying the whole sequence")

    bounded = preview(_NoFullCopy(range(25)))
    assert isinstance(bounded, list)
    assert len(bounded) == 21  # 20 kept + the marker
    assert bounded[:3] == [0, 1, 2]
    assert bounded[-1] == "+5 more"

    # A long non-JSON leaf (str fallback) is cut at the cap with the same marker
    # as a string leaf — never emitted whole.
    class _LongRepr:
        def __str__(self) -> str:
            return "y" * (TRACE_PREVIEW_MAX_CHARS + 100)

    assert preview(_LongRepr()) == "y" * TRACE_PREVIEW_MAX_CHARS + "…"


def test_trace_summary_status_required_no_usage_stays_none():
    import pydantic

    from tai42_contract.monitoring import MonitoringTraceSummary

    row = MonitoringTraceSummary(id="t1", status="ok")
    assert row.total_tokens is None
    assert row.status == "ok"
    assert row.tags == []
    assert row.input_preview is None
    # status carries no default — a row with no status fails loudly.
    with pytest.raises(pydantic.ValidationError):
        MonitoringTraceSummary(id="t1")  # pyright: ignore[reportCallIssue]


# -- MonitoringFilter range validation -----------------------------------------


def test_monitoring_filter_valid_ranges():
    f = MonitoringFilter(min_cost=1.0, max_cost=2.0, min_tokens=1, max_tokens=2, min_latency=0.1, max_latency=0.2)
    assert f.min_cost == 1.0


def test_monitoring_filter_empty_valid():
    assert MonitoringFilter().min_cost is None


def test_monitoring_filter_cost_inverted_raises():
    with pytest.raises(ValueError, match="min_cost"):
        MonitoringFilter(min_cost=2.0, max_cost=1.0)


def test_monitoring_filter_tokens_inverted_raises():
    with pytest.raises(ValueError, match="min_tokens"):
        MonitoringFilter(min_tokens=10, max_tokens=5)


def test_monitoring_filter_latency_inverted_raises():
    with pytest.raises(ValueError, match="min_latency"):
        MonitoringFilter(min_latency=2.0, max_latency=1.0)
