"""Schema → Block Kit mapping: the message button, the modal view, the coerced
answer extraction, and every loud unmappable/cap failure."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_channel_slack.forms import (
    FIELD_ACTION_ID,
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    FormSchemaError,
    build_message_blocks,
    build_modal_blocks,
    build_modal_view,
    extract_answer,
    first_field_name,
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string", "title": "Full name"},
        "tier": {"type": "string", "enum": ["gold", "silver"]},
        "subscribed": {"type": "boolean", "title": "Subscribed?"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
    },
    "required": ["full_name", "tier"],
}


def test_message_blocks_carry_section_and_open_button():
    blocks = build_message_blocks("Give us your details", "int-42")

    section, actions = blocks
    assert section == {"type": "section", "text": {"type": "plain_text", "text": "Give us your details"}}
    (button,) = actions["elements"]
    assert button["type"] == "button"
    assert button["action_id"] == FORM_OPEN_ACTION_ID
    assert button["value"] == "int-42"
    assert button["text"] == {"type": "plain_text", "text": "Fill form"}


def test_modal_view_leads_with_the_question_and_carries_metadata():
    view = build_modal_view("int-42", "Give us your details", _SCHEMA)

    assert view["type"] == "modal"
    assert view["callback_id"] == FORM_SUBMIT_CALLBACK_ID
    assert view["private_metadata"] == "int-42"
    assert view["title"] == {"type": "plain_text", "text": "Please respond"}
    assert view["submit"] == {"type": "plain_text", "text": "Submit"}
    assert view["close"] == {"type": "plain_text", "text": "Cancel"}
    assert view["blocks"][0] == {"type": "section", "text": {"type": "plain_text", "text": "Give us your details"}}
    assert [b["block_id"] for b in view["blocks"][1:]] == ["full_name", "tier", "subscribed", "count", "ratio"]


def test_modal_blocks_map_each_supported_type():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_SCHEMA)}

    assert by_id["full_name"]["label"] == {"type": "plain_text", "text": "Full name"}
    assert by_id["full_name"]["element"] == {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}

    tier = by_id["tier"]["element"]
    assert tier["type"] == "static_select"
    assert tier["action_id"] == FIELD_ACTION_ID
    assert [o["value"] for o in tier["options"]] == ["gold", "silver"]
    assert tier["options"][0]["text"] == {"type": "plain_text", "text": "gold"}

    subscribed = by_id["subscribed"]["element"]
    assert subscribed["type"] == "radio_buttons"
    assert [o["value"] for o in subscribed["options"]] == ["true", "false"]
    assert [o["text"]["text"] for o in subscribed["options"]] == ["Yes", "No"]

    assert by_id["count"]["element"] == {
        "type": "number_input",
        "action_id": FIELD_ACTION_ID,
        "is_decimal_allowed": False,
    }
    assert by_id["ratio"]["element"]["is_decimal_allowed"] is True


def test_required_fields_have_no_optional_flag_others_do():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_SCHEMA)}

    assert "optional" not in by_id["full_name"]  # required
    assert "optional" not in by_id["tier"]  # required
    assert by_id["subscribed"]["optional"] is True
    assert by_id["count"]["optional"] is True


def test_label_defaults_to_the_property_name_without_a_title():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_SCHEMA)}
    assert by_id["count"]["label"] == {"type": "plain_text", "text": "count"}


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param({"type": "array", "properties": {"a": {"type": "string"}}}, id="top-level-not-object"),
        pytest.param({"type": "object", "properties": {}}, id="empty-properties"),
        pytest.param({"type": "object"}, id="no-properties"),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"}, id="required-not-list"
        ),
    ],
)
def test_malformed_schema_shape_raises(schema):
    with pytest.raises(FormSchemaError):
        build_modal_blocks(schema)


def test_unsupported_property_type_raises_naming_property():
    schema = {"type": "object", "properties": {"payload": {"type": "array"}}}
    with pytest.raises(FormSchemaError, match=r"payload.*unsupported type"):
        build_modal_blocks(schema)


def test_enum_must_be_a_non_empty_list():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": "gold"}}}
    with pytest.raises(FormSchemaError, match=r"tier.*enum"):
        build_modal_blocks(schema)


def test_non_object_property_raises_naming_property():
    schema = {"type": "object", "properties": {"tier": "nope"}}
    with pytest.raises(FormSchemaError, match=r"tier.*must be an object"):
        build_modal_blocks(schema)


def test_over_long_label_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"note": {"type": "string", "title": "x" * 2001}}}
    with pytest.raises(FormSchemaError, match="label exceeds"):
        build_modal_blocks(schema)


def test_over_long_enum_option_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["y" * 76]}}}
    with pytest.raises(FormSchemaError, match="exceeds 75 characters"):
        build_modal_blocks(schema)


def test_too_many_options_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": [str(i) for i in range(101)]}}}
    with pytest.raises(FormSchemaError, match="enum exceeds 100 options"):
        build_modal_blocks(schema)


def test_over_long_question_section_is_a_loud_cap_error():
    with pytest.raises(FormSchemaError, match="question exceeds"):
        build_message_blocks("q" * 3001, "int-1")


def test_too_many_blocks_is_a_loud_cap_error():
    props = {f"f{i}": {"type": "string"} for i in range(100)}
    schema = {"type": "object", "properties": props}
    # 1 question section + 100 inputs = 101 blocks > the 100-block modal cap.
    with pytest.raises(FormSchemaError, match="modal exceeds 100 blocks"):
        build_modal_view("int-1", "q", schema)


def test_over_long_button_value_is_a_loud_cap_error():
    with pytest.raises(FormSchemaError, match="button value cap"):
        build_message_blocks("q", "i" * 2001)


def test_first_field_name_is_the_first_property():
    assert first_field_name(_SCHEMA) == "full_name"


def _state() -> dict[str, Any]:
    return {
        "full_name": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "Alice"}},
        "tier": {FIELD_ACTION_ID: {"type": "static_select", "selected_option": {"value": "gold"}}},
        "subscribed": {FIELD_ACTION_ID: {"type": "radio_buttons", "selected_option": {"value": "false"}}},
        "count": {FIELD_ACTION_ID: {"type": "number_input", "value": "7"}},
        "ratio": {FIELD_ACTION_ID: {"type": "number_input", "value": "1.5"}},
    }


def test_extract_answer_coerces_each_type():
    answer = extract_answer(_SCHEMA, _state())
    assert answer == {"full_name": "Alice", "tier": "gold", "subscribed": False, "count": 7, "ratio": 1.5}
    assert isinstance(answer["count"], int)
    assert isinstance(answer["ratio"], float)
    assert answer["subscribed"] is False


def test_extract_answer_true_boolean():
    state = _state()
    state["subscribed"][FIELD_ACTION_ID]["selected_option"]["value"] = "true"
    assert extract_answer(_SCHEMA, state)["subscribed"] is True


def test_extract_answer_omits_empty_optional_fields():
    state = _state()
    state["count"][FIELD_ACTION_ID]["value"] = ""
    del state["ratio"]
    answer = extract_answer(_SCHEMA, state)
    assert "count" not in answer
    assert "ratio" not in answer


def test_extract_answer_unfilled_select_is_omitted():
    state = _state()
    state["tier"][FIELD_ACTION_ID] = {"type": "static_select", "selected_option": None}
    assert "tier" not in extract_answer(_SCHEMA, state)


def test_extract_answer_uncoercible_integer_raises():
    state = _state()
    state["count"][FIELD_ACTION_ID]["value"] = "not-a-number"
    with pytest.raises(FormSchemaError, match=r"count.*not an integer"):
        extract_answer(_SCHEMA, state)


def test_extract_answer_uncoercible_number_raises():
    state = _state()
    state["ratio"][FIELD_ACTION_ID]["value"] = "abc"
    with pytest.raises(FormSchemaError, match=r"ratio.*not a number"):
        extract_answer(_SCHEMA, state)


@pytest.mark.parametrize("literal", ["1e999", "nan"])
def test_extract_answer_non_finite_number_is_forwarded_raw(literal):
    # inf/nan pass jsonschema's type:number and pydantic would store null; the
    # non-finite entry is declined so the raw string travels to the callback
    # door, where schema validation rejects it — the recovery a non-numeric
    # entry already takes.
    state = _state()
    state["ratio"][FIELD_ACTION_ID]["value"] = literal
    assert extract_answer(_SCHEMA, state)["ratio"] == literal


def test_extract_answer_unrecognized_boolean_raises():
    state = _state()
    state["subscribed"][FIELD_ACTION_ID]["selected_option"]["value"] = "maybe"
    with pytest.raises(FormSchemaError, match=r"subscribed.*boolean value not recognized"):
        extract_answer(_SCHEMA, state)


def test_extract_answer_omits_absent_and_unknown_kind_fields():
    state = _state()
    state["full_name"] = {}  # no action entry at all -> absent
    state["count"][FIELD_ACTION_ID] = {"type": "datepicker", "selected_date": "2026-01-01"}  # unknown kind
    answer = extract_answer(_SCHEMA, state)
    assert "full_name" not in answer
    assert "count" not in answer


def test_extract_answer_non_dict_schema_raises():
    schema: Any = "not-a-schema"
    with pytest.raises(FormSchemaError, match="must be an object"):
        extract_answer(schema, {})
