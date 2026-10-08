"""The monitoring encoder: one orjson encoding per value, secrets masked, markers reserved."""

from __future__ import annotations

import collections
import ipaddress
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any

import orjson
import pytest
from pydantic import BaseModel, ConfigDict
from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue

from tai42_kit.monitoring import MonitoringEncodeError, encode_payload, payload_ref, unrecorded

_RESERVED_MESSAGE = 'reserved prefix "$tai42_"'


def _round_trip(value: Any) -> Any:
    return orjson.loads(encode_payload(value))


class _Holder(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    token: Any
    note: str = "n"


def test_secret_at_depth_three_is_masked():
    value = {"a": {"b": {"c": SecretValue("hunter2")}}}
    assert _round_trip(value) == {"a": {"b": {"c": SECRET_PLACEHOLDER}}}


def test_secret_inside_a_model_is_masked():
    assert _round_trip({"m": _Holder(token=SecretValue("hunter2"))}) == {
        "m": {"token": SECRET_PLACEHOLDER, "note": "n"}
    }


def test_secret_as_a_metadata_value_is_masked():
    assert _round_trip({"api_key": SecretValue("k"), "plain": 1}) == {"api_key": SECRET_PLACEHOLDER, "plain": 1}


def test_secret_text_never_appears_in_the_encoding():
    assert "hunter2" not in encode_payload([SecretValue("hunter2"), {"x": SecretValue("hunter2")}])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.50"), "1.50"),
        (timedelta(seconds=90, milliseconds=500), 90.5),
        ({3}, [3]),
        (frozenset({"x"}), ["x"]),
        (b"\x00\x01", {"$tai42_bytes": "AAE="}),
        (bytearray(b"ab"), {"$tai42_bytes": "YWI="}),
        (ValueError("boom"), {"error_type": "ValueError", "message": "boom"}),
        (PurePosixPath("/a/b"), "/a/b"),
        (collections.deque([1, 2]), [1, 2]),
        (re.compile(r"a+b"), "a+b"),
        (ipaddress.IPv4Address("10.0.0.1"), "10.0.0.1"),
        (ipaddress.IPv6Network("2001:db8::/32"), "2001:db8::/32"),
        (ipaddress.IPv4Interface("10.0.0.1/24"), "10.0.0.1/24"),
    ],
)
def test_non_native_forms(value: Any, expected: Any):
    assert _round_trip({"v": value}) == {"v": expected}


def test_payload_ref_renders_as_its_one_key_object():
    ref = payload_ref("b" * 16, "output", "/x/1")
    assert _round_trip({"y": ref}) == {"y": {"$tai42_ref": {"span_id": "b" * 16, "field": "output", "pointer": "/x/1"}}}


def test_payload_ref_with_trace_renders_the_trace():
    ref = payload_ref("b" * 16, "input", trace_id="a" * 32)
    assert _round_trip(ref) == {
        "$tai42_ref": {"trace_id": "a" * 32, "span_id": "b" * 16, "field": "input", "pointer": ""}
    }


def test_unrecorded_renders_as_its_one_key_object():
    assert _round_trip([unrecorded(), unrecorded()]) == [{"$tai42_unrecorded": True}, {"$tai42_unrecorded": True}]


def test_hand_built_reference_is_refused():
    with pytest.raises(MonitoringEncodeError, match=re.escape(_RESERVED_MESSAGE)):
        encode_payload({"$tai42_ref": "x"})


def test_hand_built_unrecorded_marker_at_depth_two_is_refused():
    with pytest.raises(MonitoringEncodeError, match=re.escape(_RESERVED_MESSAGE)):
        encode_payload({"a": {"b": {"$tai42_unrecorded": True}}})


def test_hand_built_marker_beside_a_real_marker_is_refused():
    with pytest.raises(MonitoringEncodeError, match=re.escape(_RESERVED_MESSAGE)):
        encode_payload([payload_ref("s", "input"), {"$tai42_ref": {"span_id": "s", "field": "input"}}])


def test_marker_text_inside_a_string_is_data():
    assert _round_trip({"s": '{"$tai42_ref": 1}'}) == {"s": '{"$tai42_ref": 1}'}


def test_reserved_prefix_as_a_second_key_is_data():
    assert _round_trip({"a": 1, "$tai42_x": 2}) == {"a": 1, "$tai42_x": 2}


def test_unknown_class_is_refused_naming_the_type():
    class Opaque:
        pass

    with pytest.raises(MonitoringEncodeError, match="Opaque"):
        encode_payload({"o": Opaque()})


def test_out_of_range_int_is_refused():
    with pytest.raises(MonitoringEncodeError):
        encode_payload({"n": 2**70})


def test_int_dict_keys_encode_as_strings():
    assert _round_trip({1: "a", 2: {3: "b"}}) == {"1": "a", "2": {"3": "b"}}


def test_encode_error_is_a_type_error():
    assert issubclass(MonitoringEncodeError, TypeError)
