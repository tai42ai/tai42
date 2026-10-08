"""Tests for the monitoring models: the vendor-neutral trace metadata, the reader's
non-optional return contracts, the declared list capability and the neutral observation shape."""

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


def test_observation_carries_a_neutral_kind_and_typed_token_counts():
    from tai42_contract.monitoring import MonitoringObservation, SpanKind

    obs = MonitoringObservation(id="o1", kind=SpanKind.LLM, input_tokens=12, output_tokens=3, total_tokens=15)
    assert obs.kind is SpanKind.LLM
    assert (obs.input_tokens, obs.output_tokens, obs.total_tokens) == (12, 3, 15)
    assert set(MonitoringObservation.model_fields) == {
        "id",
        "trace_id",
        "parent_id",
        "kind",
        "name",
        "level",
        "status_message",
        "input",
        "output",
        "metadata",
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "start",
        "end",
    }


def test_observation_kind_accepts_only_the_neutral_kinds():
    import pydantic

    from tai42_contract.monitoring import MonitoringObservation

    assert MonitoringObservation.model_validate({"id": "o1", "kind": "TOOL"}).kind == "TOOL"
    with pytest.raises(pydantic.ValidationError):
        MonitoringObservation.model_validate({"id": "o1", "kind": "GENERATION"})


def test_list_capability_is_a_frozen_declaration():
    import pydantic

    from tai42_contract.monitoring import ListCapability

    cap = ListCapability(
        sort_fields=frozenset({"timestamp", "latency"}),
        incompatible_filters={"latency": frozenset({"user_id"})},
    )
    assert cap.sort_fields == {"timestamp", "latency"}
    assert cap.incompatible_filters == {"latency": {"user_id"}}
    with pytest.raises(pydantic.ValidationError):
        cap.sort_fields = frozenset()  # pyright: ignore[reportAttributeAccessIssue]


def test_reader_declares_its_list_capability_and_page_ceiling():
    import typing

    from tai42_contract.monitoring import ListCapability
    from tai42_contract.monitoring.reader import MonitoringReader

    members = typing.get_protocol_members(MonitoringReader)
    assert {"list_capability", "max_page_size"} <= members
    assert typing.get_type_hints(MonitoringReader.list_capability)["return"] is ListCapability
    assert typing.get_type_hints(MonitoringReader.max_page_size)["return"] is int


def test_trace_summary_carries_raw_input_and_output():
    from tai42_contract.monitoring import MonitoringTraceSummary

    raw = {"prompt": "x" * 10_000, "items": list(range(50))}
    row = MonitoringTraceSummary(id="t1", status="ok", input=raw, output="done")
    assert row.input == raw
    assert row.output == "done"


def test_trace_summary_status_required_no_usage_stays_none():
    import pydantic

    from tai42_contract.monitoring import MonitoringTraceSummary

    row = MonitoringTraceSummary(id="t1", status="ok")
    assert row.total_tokens is None
    assert row.status == "ok"
    assert row.tags == []
    assert row.input is None
    assert row.output is None
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


def test_span_window_item_tags_available_defaults_true():
    from datetime import datetime

    from tai42_contract.monitoring.models import SpanWindowItem

    item = SpanWindowItem(id="x", start=datetime(2026, 1, 1))
    assert item.tags == []
    assert item.tags_available is True


def test_span_window_item_tags_unavailable_is_distinct_from_empty():
    from datetime import datetime

    from tai42_contract.monitoring.models import SpanWindowItem

    # 'unavailable' is not 'empty': tags_available=False marks a failed tag
    # fetch, while an empty tags list means the span is genuinely untagged.
    item = SpanWindowItem(id="x", start=datetime(2026, 1, 1), tags=[], tags_available=False)
    assert item.tags == []
    assert item.tags_available is False
