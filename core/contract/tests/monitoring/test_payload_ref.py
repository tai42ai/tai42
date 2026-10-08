"""The record-reference and record-identity models of the monitoring contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.monitoring import (
    PAYLOAD_REF_KEY,
    RESERVED_KEY_PREFIX,
    UNRECORDED_KEY,
    PayloadRef,
    RecordId,
)


def test_payload_ref_round_trips_through_json():
    ref = PayloadRef(trace_id="a" * 32, span_id="b" * 16, field="output", pointer="/x/1")
    assert PayloadRef.model_validate_json(ref.model_dump_json()) == ref


def test_payload_ref_defaults_to_the_carrying_trace_and_the_whole_value():
    ref = PayloadRef(span_id="b" * 16, field="input")
    assert ref.trace_id is None
    assert ref.pointer == ""


def test_payload_ref_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        PayloadRef.model_validate({"span_id": "s", "field": "input", "size": 3})


def test_payload_ref_accepts_the_metadata_field():
    assert PayloadRef(span_id="s", field="metadata", pointer="/tai42.message").field == "metadata"


@pytest.mark.parametrize("field", ["usage", "attributes", "INPUT", ""])
def test_payload_ref_refuses_any_other_field(field: str):
    with pytest.raises(ValidationError):
        PayloadRef.model_validate({"span_id": "s", "field": field})


def test_payload_ref_is_frozen():
    ref = PayloadRef(span_id="s", field="input")
    with pytest.raises(ValidationError):
        ref.span_id = "t"  # type: ignore[misc]


def test_record_id_is_frozen_and_carries_both_ids():
    rid = RecordId(trace_id="a" * 32, span_id="b" * 16)
    assert (rid.trace_id, rid.span_id) == ("a" * 32, "b" * 16)
    with pytest.raises(ValidationError):
        rid.span_id = "c"  # type: ignore[misc]


def test_marker_keys_share_the_reserved_prefix():
    assert RESERVED_KEY_PREFIX == "$tai42_"
    assert PAYLOAD_REF_KEY == "$tai42_ref"
    assert UNRECORDED_KEY == "$tai42_unrecorded"
    assert PAYLOAD_REF_KEY.startswith(RESERVED_KEY_PREFIX)
    assert UNRECORDED_KEY.startswith(RESERVED_KEY_PREFIX)
