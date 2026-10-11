"""Tests for the record id predicates: a trace id is 32 lower-hex, a span id 16 lower-hex, neither all zeros."""

from __future__ import annotations

import uuid

import pytest

from tai42_contract.monitoring import SPAN_ID_PATTERN, TRACE_ID_PATTERN, is_span_id, is_trace_id


def test_a_uuid_hex_is_a_trace_id() -> None:
    assert is_trace_id(uuid.uuid4().hex)


@pytest.mark.parametrize(
    "value",
    [
        str(uuid.uuid4()),  # the dashed form
        uuid.uuid4().hex.upper(),
        "0" * 32,
        "a" * 31,
        "a" * 33,
        "g" * 32,
        "",
        None,
        int("1" * 32, 16),
    ],
)
def test_a_value_that_is_not_32_lower_hex_or_is_all_zeros_is_not_a_trace_id(value: object) -> None:
    assert not is_trace_id(value)


def test_sixteen_lower_hex_not_all_zeros_is_a_span_id() -> None:
    assert is_span_id("0" * 15 + "1")
    assert is_span_id(uuid.uuid4().hex[:16])


@pytest.mark.parametrize(
    "value",
    ["0" * 16, "a" * 15, "a" * 17, "A" * 16, "short", uuid.uuid4().hex, None, 1],
)
def test_a_value_that_is_not_16_lower_hex_or_is_all_zeros_is_not_a_span_id(value: object) -> None:
    assert not is_span_id(value)


def test_the_patterns_state_the_widths() -> None:
    assert TRACE_ID_PATTERN.fullmatch("f" * 32)
    assert not TRACE_ID_PATTERN.fullmatch("f" * 16)
    assert SPAN_ID_PATTERN.fullmatch("f" * 16)
    assert not SPAN_ID_PATTERN.fullmatch("f" * 32)
