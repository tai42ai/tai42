"""The monitoring contract's key constants, markers, health model and protocol surface."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from tai42_contract.errors import ErrorKind
from tai42_contract.monitoring import (
    GENERATION_MESSAGE_METADATA_KEY,
    MONITORING_PARENT_SPAN_ID_KEY,
    MONITORING_TRACE_ID_KEY,
    PROMOTED_METADATA_KEYS,
    STEP_ROLE_METADATA_KEY,
    TIMING_ABSENT,
    TIMING_METADATA_KEY,
    MonitoringError,
    MonitoringExportHealth,
    MonitoringReader,
    MonitoringWriter,
    ObservationNotFoundError,
    PayloadRefUnresolvedError,
    Span,
    StepRole,
    TokenUsage,
)


def test_lineage_keys():
    assert MONITORING_TRACE_ID_KEY == "monitoring_trace_id"
    assert MONITORING_PARENT_SPAN_ID_KEY == "monitoring_parent_span_id"


def test_step_role_values():
    assert StepRole.GROUPING == "grouping"
    assert StepRole.SUB_STEP == "sub_step"
    assert {r.value for r in StepRole} == {"grouping", "sub_step"}


def test_promoted_metadata_keys():
    assert STEP_ROLE_METADATA_KEY == "tai42.step_role"
    assert TIMING_METADATA_KEY == "tai42.timing"
    assert TIMING_ABSENT == "absent"
    assert frozenset({"tai42.step_role", "tai42.timing"}) == PROMOTED_METADATA_KEYS


def test_generation_message_key_is_not_promoted():
    assert GENERATION_MESSAGE_METADATA_KEY == "tai42.message"
    assert GENERATION_MESSAGE_METADATA_KEY not in PROMOTED_METADATA_KEYS


def test_token_usage_defaults_and_frozen():
    usage = TokenUsage(input_tokens=1, output_tokens=2)
    assert usage.model_dump() == {"input_tokens": 1, "output_tokens": 2, "total_tokens": None, "cost_usd": None}
    with pytest.raises(ValidationError):
        usage.input_tokens = 3  # type: ignore[misc]


def test_export_health_starts_at_zero():
    health = MonitoringExportHealth()
    assert health.model_dump() == {
        "spans_dropped": 0,
        "spans_failed": 0,
        "export_failures": 0,
        "attributes_dropped": 0,
        "records_failed": 0,
        "last_error": None,
    }
    with pytest.raises(ValidationError):
        health.spans_dropped = 1  # type: ignore[misc]


def test_new_errors_carry_their_kinds():
    assert issubclass(ObservationNotFoundError, MonitoringError)
    assert issubclass(PayloadRefUnresolvedError, MonitoringError)
    assert ObservationNotFoundError.__tai_error_kind__ is ErrorKind.NOT_FOUND
    assert PayloadRefUnresolvedError.__tai_error_kind__ is ErrorKind.UPSTREAM_ERROR


def test_writer_protocol_members():
    assert typing.get_protocol_members(MonitoringWriter) == frozenset(
        {
            "open_span",
            "start_span",
            "record_span",
            "create_event",
            "update_current_span",
            "trace_attributes",
            "current_trace_id",
            "current_span_id",
            "is_recording",
            "export_health",
            "set_health_listener",
            "disable",
            "flush",
            "shutdown",
        }
    )


def test_span_protocol_members():
    assert typing.get_protocol_members(Span) == frozenset({"id", "update", "set_trace_metadata", "end"})


def test_span_update_takes_typed_usage():
    hints = typing.get_type_hints(Span.update)
    assert hints["usage"] == TokenUsage | None


def test_record_span_takes_typed_usage():
    hints = typing.get_type_hints(MonitoringWriter.record_span)
    assert hints["usage"] == TokenUsage | None


def test_reader_has_get_observation():
    assert "get_observation" in typing.get_protocol_members(MonitoringReader)
