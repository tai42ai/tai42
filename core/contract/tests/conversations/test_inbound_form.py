"""Tests for the opaque-transport bounds: ``validate_bounded_object`` /
``validate_inbound_form`` and the ``entry_params`` transport vocabulary re-exported here."""

from __future__ import annotations

from typing import Any

import pytest

# -- validate_entry_params ------------------------------------------------------


def test_validate_entry_params_returns_a_clean_dict_unchanged():
    from tai42_contract.conversations import validate_entry_params

    clean = {"token": "abc-123", "ref": "x_9", "Mixed-CASE": "v"}
    assert validate_entry_params(clean) is clean


def test_validate_entry_params_accepts_the_empty_dict():
    from tai42_contract.conversations import validate_entry_params

    assert validate_entry_params({}) == {}


def test_validate_entry_params_refuses_too_many_keys():
    from tai42_contract.conversations import ENTRY_PARAMS_MAX_COUNT, validate_entry_params

    too_many = {f"k{i}": "v" for i in range(ENTRY_PARAMS_MAX_COUNT + 1)}
    with pytest.raises(ValueError, match=f"over the {ENTRY_PARAMS_MAX_COUNT} allowed"):
        validate_entry_params(too_many)


@pytest.mark.parametrize("bad_key", ["has space", "sym$bol", "dot.dot", "slash/es", "", "café"])
def test_validate_entry_params_refuses_a_bad_key_charset(bad_key: str):
    from tai42_contract.conversations import validate_entry_params

    with pytest.raises(ValueError, match="must match"):
        validate_entry_params({bad_key: "v"})


def test_validate_entry_params_refuses_an_over_long_key():
    from tai42_contract.conversations import validate_entry_params

    with pytest.raises(ValueError, match="must match"):
        validate_entry_params({"k" * 65: "v"})


def test_validate_entry_params_refuses_a_non_str_value():
    from tai42_contract.conversations import validate_entry_params

    with pytest.raises(ValueError, match="must be a string"):
        validate_entry_params({"k": 1})  # type: ignore[dict-item]


def test_validate_entry_params_refuses_an_over_long_value():
    from tai42_contract.conversations import ENTRY_PARAM_VALUE_MAX_CHARS, validate_entry_params

    with pytest.raises(ValueError, match="character limit"):
        validate_entry_params({"k": "v" * (ENTRY_PARAM_VALUE_MAX_CHARS + 1)})


def test_validate_entry_params_refuses_an_over_size_total():
    from tai42_contract.conversations import ENTRY_PARAM_VALUE_MAX_CHARS, validate_entry_params

    # Each value is under the per-value cap, but enough of them push the serialized total
    # over the byte budget — the bound the transport cap is really about.
    over_total = {f"k{i:02d}": "v" * ENTRY_PARAM_VALUE_MAX_CHARS for i in range(8)}
    with pytest.raises(ValueError, match="bytes, over"):
        validate_entry_params(over_total)


def test_validate_entry_params_error_never_carries_a_value():
    from tai42_contract.conversations import validate_entry_params

    secret = "super-secret-token-value"
    with pytest.raises(ValueError, match="character limit") as excinfo:
        validate_entry_params({"k": secret + "x" * 512})
    assert secret not in str(excinfo.value)


# -- validate_inbound_form / validate_bounded_object ----------------------------


def test_validate_inbound_form_returns_a_clean_dict_unchanged():
    from tai42_contract.conversations import validate_inbound_form

    form = {"size": "L", "count": 2, "nested": {"ok": True, "tags": ["a", "b"], "note": None}}
    assert validate_inbound_form(form) is form


@pytest.mark.parametrize("bad", [["a"], "scalar", 1, 1.5, True, None])
def test_validate_inbound_form_refuses_non_objects(bad: Any):
    # The submission is a JSON OBJECT — a list or scalar top level is refused.
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="must be a JSON object"):
        validate_inbound_form(bad)


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), float("-inf")])
def test_validate_inbound_form_refuses_non_finite_numbers(bad_number: float):
    # NaN/Infinity are not JSON; a consumer parsing the stored form must never hit them.
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="must be finite"):
        validate_inbound_form({"x": bad_number})


def test_validate_inbound_form_refuses_non_serializable_values():
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="JSON-serializable"):
        validate_inbound_form({"x": object()})


def test_validate_inbound_form_refuses_non_string_keys():
    # A non-string key would be silently coerced by serialization, altering the submission.
    from tai42_contract.conversations import validate_inbound_form

    with pytest.raises(ValueError, match="keys must be strings"):
        validate_inbound_form({1: "x"})


def test_validate_inbound_form_size_cap_counts_utf8_bytes():
    from tai42_contract.conversations import INBOUND_FORM_MAX_BYTES, validate_inbound_form

    # Just under the cap in ASCII passes; the same character count in a multi-byte
    # alphabet is over it — the bound is UTF-8 BYTES, not characters.
    char_count = INBOUND_FORM_MAX_BYTES - 100
    assert validate_inbound_form({"x": "a" * char_count}) is not None
    with pytest.raises(ValueError, match=f"over the {INBOUND_FORM_MAX_BYTES} allowed"):
        validate_inbound_form({"x": "é" * char_count})


def test_validate_inbound_form_deeply_nested_is_a_clean_refusal():
    # A pathological nesting well under the byte cap must refuse with a plain ValueError
    # naming the depth bound — never surface a RecursionError from the interpreter stack.
    from tai42_contract.conversations import INBOUND_FORM_MAX_DEPTH, validate_inbound_form

    deep: dict[str, Any] = {}
    node = deep
    for _ in range(5000):
        child: dict[str, Any] = {}
        node["a"] = child
        node = child
    with pytest.raises(ValueError, match=f"deeper than the {INBOUND_FORM_MAX_DEPTH}"):
        validate_inbound_form(deep)


def test_validate_inbound_form_accepts_nesting_at_the_depth_bound():
    from tai42_contract.conversations import INBOUND_FORM_MAX_DEPTH, validate_inbound_form

    at_bound: dict[str, Any] = {}
    node = at_bound
    for _ in range(INBOUND_FORM_MAX_DEPTH - 1):
        child: dict[str, Any] = {}
        node["a"] = child
        node = child
    assert validate_inbound_form(at_bound) is at_bound
    over = {"a": at_bound}
    with pytest.raises(ValueError, match="deeper than"):
        validate_inbound_form(over)


def test_validate_inbound_form_error_never_carries_a_value():
    # Form contents are opaque participant data: no refusal message may quote a submitted value.
    from tai42_contract.conversations import validate_inbound_form

    marker = "secret-answer-material"
    for bad in ({marker: object()}, {"k": marker + "!" * (32 * 1024)}, {1: marker}):
        with pytest.raises(ValueError, match="form") as exc_info:
            validate_inbound_form(bad)
        assert marker not in str(exc_info.value)


def test_validate_bounded_object_names_the_object_in_its_messages():
    # The shared validator carries ``what`` into every refusal message so an event payload
    # refusal names the payload, not "form".
    from tai42_contract.conversations import validate_bounded_object

    with pytest.raises(ValueError, match="event payload must be a JSON object"):
        validate_bounded_object(["a"], what="event payload")


def test_validate_inbound_form_delegates_to_the_shared_validator():
    # ``validate_inbound_form`` is the ``what="form"`` binding of the shared validator; its
    # messages stay byte-for-byte identical (the existing form tests above pin that).
    from tai42_contract.conversations import validate_bounded_object, validate_inbound_form

    form = {"a": 1}
    assert validate_inbound_form(form) is validate_bounded_object(form, what="form")
