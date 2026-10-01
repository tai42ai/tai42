"""Schema → Block Kit mapping: the message button, the modal view, the coerced
answer extraction, and every loud unmappable/cap failure."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_channel_slack.forms import (
    FIELD_ACTION_ID,
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    RADIO_OPTION_THRESHOLD,
    FormSchemaError,
    build_message_blocks,
    build_modal_blocks,
    build_modal_view,
    decode_private_metadata,
    extract_answer,
    first_field_name,
    validate_form_schema,
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
    # private_metadata carries the interaction id (plus any accumulated reaction state) as JSON.
    assert decode_private_metadata(view["private_metadata"]) == ("int-42", {}, {})
    assert view["title"] == {"type": "plain_text", "text": "Please respond"}
    assert view["submit"] == {"type": "plain_text", "text": "Submit"}
    assert view["close"] == {"type": "plain_text", "text": "Cancel"}
    assert view["blocks"][0] == {"type": "section", "text": {"type": "plain_text", "text": "Give us your details"}}
    assert [b["block_id"] for b in view["blocks"][1:]] == ["full_name", "tier", "subscribed", "count", "ratio"]


def test_modal_prefills_each_control_from_the_per_send_values():
    values = {"full_name": "Ada", "tier": "gold", "subscribed": True, "count": 3, "ratio": 1.5}
    by_id = {b["block_id"]: b for b in build_modal_blocks(_SCHEMA, values)}

    assert by_id["full_name"]["element"]["initial_value"] == "Ada"
    assert by_id["count"]["element"]["initial_value"] == "3"
    assert by_id["ratio"]["element"]["initial_value"] == "1.5"
    # A select prefills its initial_option to the matching {text, value}.
    assert by_id["tier"]["element"]["initial_option"] == {
        "text": {"type": "plain_text", "text": "gold"},
        "value": "gold",
    }
    # The Yes/No radio prefills the "true" option for a boolean True.
    assert by_id["subscribed"]["element"]["initial_option"]["value"] == "true"


def test_per_send_options_build_a_labelled_choice_replacing_the_field_control():
    schema = {"type": "object", "properties": {"colour": {"type": "string", "title": "Colour"}}}
    options = {"colour": [{"value": "r", "label": "Red"}, {"value": "b", "label": "Blue"}]}
    (block,) = build_modal_blocks(schema, {}, options)

    element = block["element"]
    # 2 per-send options are at or below the threshold -> a radio group (a longer list is a select).
    assert element["type"] == "radio_buttons"
    # Labels shown, values submitted.
    assert element["options"] == [
        {"text": {"type": "plain_text", "text": "Red"}, "value": "r"},
        {"text": {"type": "plain_text", "text": "Blue"}, "value": "b"},
    ]


def test_long_per_send_option_list_renders_a_select_above_the_threshold():
    schema = {"type": "object", "properties": {"colour": {"type": "string"}}}
    options = {"colour": [{"value": f"c{i}"} for i in range(RADIO_OPTION_THRESHOLD + 1)]}
    (block,) = build_modal_blocks(schema, {}, options)
    assert block["element"]["type"] == "static_select"


def test_per_send_option_without_a_label_shows_its_value():
    schema = {"type": "object", "properties": {"colour": {"type": "string"}}}
    (block,) = build_modal_blocks(schema, {}, {"colour": [{"value": "r"}]})
    assert block["element"]["options"] == [{"text": {"type": "plain_text", "text": "r"}, "value": "r"}]


def test_per_send_options_prefilled_value_selects_the_matching_option():
    schema = {"type": "object", "properties": {"colour": {"type": "string"}}}
    options = {"colour": [{"value": "r", "label": "Red"}, {"value": "b", "label": "Blue"}]}
    (block,) = build_modal_blocks(schema, {"colour": "b"}, options)
    assert block["element"]["initial_option"] == {"text": {"type": "plain_text", "text": "Blue"}, "value": "b"}


def test_per_send_options_on_a_non_string_property_raise_naming_it():
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    with pytest.raises(FormSchemaError, match="count"):
        build_modal_blocks(schema, {}, {"count": [{"value": "1"}]})


def test_prefilled_select_value_not_among_options_raises_naming_it():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["gold", "silver"]}}}
    with pytest.raises(FormSchemaError, match="tier"):
        build_modal_blocks(schema, {"tier": "bronze"})


def test_pages_render_as_titled_header_sections_in_one_modal():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    pages = [{"title": "First", "fields": ["a"]}, {"title": "Second", "fields": ["b"]}]
    blocks = build_modal_blocks(schema, {}, {}, pages)

    kinds = [(b["type"], b.get("text", {}).get("text") or b.get("block_id")) for b in blocks]
    assert kinds == [("header", "First"), ("input", "a"), ("header", "Second"), ("input", "b")]


def test_pages_naming_an_unknown_property_raise():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pytest.raises(FormSchemaError, match="ghost"):
        build_modal_blocks(schema, {}, {}, [{"title": "P", "fields": ["ghost"]}])


def test_modal_blocks_map_each_supported_type():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_SCHEMA)}

    assert by_id["full_name"]["label"] == {"type": "plain_text", "text": "Full name"}
    assert by_id["full_name"]["element"] == {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}

    tier = by_id["tier"]["element"]
    # A 2-option enum is at or below RADIO_OPTION_THRESHOLD, so it renders as a radio group.
    assert tier["type"] == "radio_buttons"
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


_FORMAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "on": {"type": "string", "format": "date", "title": "On"},
        "at": {"type": "string", "format": "time", "title": "At"},
        "when": {"type": "string", "format": "date-time", "title": "When"},
    },
}


def test_date_format_renders_a_native_datepicker():
    element = build_modal_blocks(_FORMAT_SCHEMA)[0]["element"]
    assert element == {"type": "datepicker", "action_id": FIELD_ACTION_ID}


def test_time_format_renders_a_native_timepicker():
    element = build_modal_blocks(_FORMAT_SCHEMA)[1]["element"]
    assert element == {"type": "timepicker", "action_id": FIELD_ACTION_ID}


def test_date_time_format_stays_a_plain_text_input():
    element = build_modal_blocks(_FORMAT_SCHEMA)[2]["element"]
    assert element == {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}


def test_date_and_time_prefills_ride_the_native_initial_fields():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_FORMAT_SCHEMA, {"on": "2026-01-31", "at": "09:05"})}
    assert by_id["on"]["element"]["initial_date"] == "2026-01-31"
    assert by_id["at"]["element"]["initial_time"] == "09:05"


@pytest.mark.parametrize("bad", ["2026/01/31", "31-01-2026", "2026-1-1", "2026-13-01", "not-a-date", ""])
def test_invalid_date_prefill_is_refused_naming_the_field(bad):
    with pytest.raises(FormSchemaError, match="on"):
        build_modal_blocks(_FORMAT_SCHEMA, {"on": bad})


@pytest.mark.parametrize("bad", ["09:05:00", "9:5", "24:00", "12:60", "0905", "noon", ""])
def test_invalid_time_prefill_is_refused_naming_the_field(bad):
    with pytest.raises(FormSchemaError, match="at"):
        build_modal_blocks(_FORMAT_SCHEMA, {"at": bad})


def test_time_prefill_with_seconds_is_refused_stating_slack_cannot_show_seconds():
    with pytest.raises(FormSchemaError, match="seconds"):
        build_modal_blocks(_FORMAT_SCHEMA, {"at": "09:05:30"})


def test_per_send_options_win_over_a_date_format_string():
    # A string with per-send options is a choice control regardless of its format.
    schema = {"type": "object", "properties": {"on": {"type": "string", "format": "date"}}}
    (block,) = build_modal_blocks(schema, {}, {"on": [{"value": "soon", "label": "Soon"}]})
    assert block["element"]["type"] == "radio_buttons"


def test_extract_answer_reads_date_and_time_pickers_unchanged():
    state = {
        "on": {FIELD_ACTION_ID: {"type": "datepicker", "selected_date": "2026-01-31"}},
        "at": {FIELD_ACTION_ID: {"type": "timepicker", "selected_time": "09:05"}},
        "when": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "2026-01-31T09:05:00Z"}},
    }
    answer = extract_answer(_FORMAT_SCHEMA, state)
    # The vendor's ISO strings return verbatim — no coercion.
    assert answer == {"on": "2026-01-31", "at": "09:05", "when": "2026-01-31T09:05:00Z"}


@pytest.mark.parametrize(("kind", "key"), [("datepicker", "selected_date"), ("timepicker", "selected_time")])
def test_extract_answer_omits_an_empty_picker(kind, key):
    state = {"on": {FIELD_ACTION_ID: {"type": kind, key: None}}}
    assert extract_answer({"type": "object", "properties": {"on": {"type": "string", "format": "date"}}}, state) == {}


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
    schema = {"type": "object", "properties": {"payload": {"type": "object"}}}
    with pytest.raises(FormSchemaError, match=r"payload.*unsupported type"):
        build_modal_blocks(schema)


def test_array_without_string_items_is_refused_naming_property():
    # An array whose items are not strings has no Block Kit control — refused naming it.
    schema = {"type": "object", "properties": {"payload": {"type": "array"}}}
    with pytest.raises(FormSchemaError, match=r"payload.*array of strings"):
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
    with pytest.raises(FormSchemaError, match="options exceed 100"):
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
    # An element kind this reader does not handle (the plugin never emits a checkbox group)
    # yields no value -> the field is omitted, never guessed at.
    state["count"][FIELD_ACTION_ID] = {"type": "checkboxes", "selected_options": []}
    answer = extract_answer(_SCHEMA, state)
    assert "full_name" not in answer
    assert "count" not in answer


def test_extract_answer_non_dict_schema_raises():
    schema: Any = "not-a-schema"
    with pytest.raises(FormSchemaError, match="must be an object"):
        extract_answer(schema, {})


# -- E: multiple choice (array) + the radio/select threshold --------------------------------


def _array_schema(enum: list[str] | None = None) -> dict[str, Any]:
    items: dict[str, Any] = {"type": "string"}
    if enum is not None:
        items["enum"] = enum
    return {"type": "object", "properties": {"tags": {"type": "array", "items": items, "title": "Tags"}}}


def test_short_array_enum_renders_checkboxes():
    (block,) = build_modal_blocks(_array_schema(["a", "b", "c"]))
    assert block["element"]["type"] == "checkboxes"
    assert [o["value"] for o in block["element"]["options"]] == ["a", "b", "c"]


def test_long_array_enum_renders_multi_static_select():
    (block,) = build_modal_blocks(_array_schema([str(i) for i in range(RADIO_OPTION_THRESHOLD + 1)]))
    assert block["element"]["type"] == "multi_static_select"


def test_free_array_renders_a_multiline_text_input():
    (block,) = build_modal_blocks(_array_schema())
    assert block["element"] == {"type": "plain_text_input", "action_id": FIELD_ACTION_ID, "multiline": True}


def test_array_per_send_options_build_a_multiple_choice_control():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    options = {"tags": [{"value": "r", "label": "Red"}, {"value": "b", "label": "Blue"}]}
    (block,) = build_modal_blocks(schema, {}, options)
    assert block["element"]["type"] == "checkboxes"
    assert block["element"]["options"] == [
        {"text": {"type": "plain_text", "text": "Red"}, "value": "r"},
        {"text": {"type": "plain_text", "text": "Blue"}, "value": "b"},
    ]


def test_extract_answer_returns_a_list_for_a_multi_select_field():
    schema = _array_schema(["a", "b", "c"])
    state = {"tags": {FIELD_ACTION_ID: {"type": "checkboxes", "selected_options": [{"value": "a"}, {"value": "c"}]}}}
    assert extract_answer(schema, state) == {"tags": ["a", "c"]}


def test_extract_answer_returns_a_list_from_a_free_array_text_area():
    schema = _array_schema()
    state = {"tags": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "one\n two \n\nthree"}}}
    # One entry per non-blank line, each trimmed.
    assert extract_answer(schema, state) == {"tags": ["one", "two", "three"]}


def test_extract_answer_omits_an_empty_multi_select():
    schema = _array_schema(["a", "b"])
    state = {"tags": {FIELD_ACTION_ID: {"type": "multi_static_select", "selected_options": []}}}
    assert extract_answer(schema, state) == {}


def test_multi_select_prefill_rides_initial_options():
    schema = _array_schema(["a", "b", "c"])
    (block,) = build_modal_blocks(schema, {"tags": ["a", "c"]})
    assert block["element"]["initial_options"] == [
        {"text": {"type": "plain_text", "text": "a"}, "value": "a"},
        {"text": {"type": "plain_text", "text": "c"}, "value": "c"},
    ]


def test_multi_select_prefill_value_not_among_options_raises_naming_it():
    with pytest.raises(FormSchemaError, match="tags"):
        build_modal_blocks(_array_schema(["a", "b"]), {"tags": ["z"]})


def test_free_array_prefill_joins_one_entry_per_line():
    (block,) = build_modal_blocks(_array_schema(), {"tags": ["x", "y"]})
    assert block["element"]["initial_value"] == "x\ny"


# -- C: dates are bare pickers (bounds enforced on submit, never drawn) ----------------------


def test_bounded_date_renders_a_bare_datepicker_no_control_bounds():
    # Slack's datepicker draws no min/max/disabled; the declared bound is enforced on submit.
    schema = {
        "type": "object",
        "properties": {"on": {"type": "string", "format": "date", "minDate": "2026-01-01", "maxDate": "2026-12-31"}},
    }
    (block,) = build_modal_blocks(schema)
    assert block["element"] == {"type": "datepicker", "action_id": FIELD_ACTION_ID}


def test_date_range_renders_two_bare_datepickers():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 1, "maxDays": 30},
        },
    }
    by_id = {b["block_id"]: b["element"] for b in build_modal_blocks(schema)}
    assert by_id["start"] == {"type": "datepicker", "action_id": FIELD_ACTION_ID}
    assert by_id["end"] == {"type": "datepicker", "action_id": FIELD_ACTION_ID}


# -- D: display blocks + review step ---------------------------------------------------------


def test_display_blocks_and_review_render_interleaved_by_page_order():
    schema = {"type": "object", "properties": {"name": {"type": "string", "title": "Name"}}}
    pages = [
        {
            "title": "Intro",
            "fields": ["name"],
            "display": [
                {"kind": "heading", "text": "Welcome"},
                {"kind": "body", "text": "Fill this in"},
                {"kind": "image", "src": "https://x/y.png", "alt": "logo"},
            ],
        },
        {"title": "Review", "fields": [], "kind": "review", "display": [{"kind": "body", "text": "Confirm below"}]},
    ]
    blocks = build_modal_blocks(schema, {"name": "Ada"}, None, pages)
    assert [b["type"] for b in blocks] == [
        "header",  # page title Intro
        "header",  # heading display
        "section",  # body display
        "image",  # image display
        "input",  # name
        "header",  # page title Review
        "section",  # body display
        "section",  # the review readback
    ]
    assert blocks[3] == {"type": "image", "image_url": "https://x/y.png", "alt_text": "logo"}
    assert blocks[-1]["text"] == {"type": "mrkdwn", "text": "*Name*: Ada"}


def test_review_readback_shows_a_placeholder_when_nothing_is_entered():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    pages = [
        {"title": "Fill", "fields": ["name"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    blocks = build_modal_blocks(schema, {}, None, pages)
    assert blocks[-1]["text"]["text"] == "No answers to review yet."


def test_slotted_display_block_is_empty_until_a_value_fills_it():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    pages = [{"title": "P", "fields": ["x"], "display": [{"kind": "body", "slot": "total"}]}]
    # No value for the slot yet -> the block renders nothing (it fills on a reaction).
    assert [b["type"] for b in build_modal_blocks(schema, {}, None, pages)] == ["header", "input"]
    # A filled slot renders the body section (a computed total shows here).
    filled = build_modal_blocks(schema, {}, None, pages, display_values={"total": "Total: 42"})
    assert filled[1] == {"type": "section", "text": {"type": "mrkdwn", "text": "Total: 42"}}


def test_slotted_image_with_no_source_degrades_to_its_alt_text():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    pages = [{"title": "P", "fields": ["x"], "display": [{"kind": "image", "slot": "chart", "alt": "a chart"}]}]
    blocks = build_modal_blocks(schema, {}, None, pages)
    assert blocks[1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": "a chart"}]}


# -- B: conditional show/hide, evaluated here (platform logic) -------------------------------


_COND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["a", "b"]},
        "detail": {"type": "string", "visibleWhen": {"field": "kind", "equals": "a"}},
    },
}


def test_conditional_controller_carries_dispatch_action_and_shows_all_without_values():
    # values None -> every field shows (the show-all base / cap upper bound); the controlling
    # field carries dispatch_action so a change reaches the re-render.
    by_id = {b["block_id"]: b for b in build_modal_blocks(_COND_SCHEMA)}
    assert "detail" in by_id
    assert by_id["kind"]["dispatch_action"] is True
    assert "dispatch_action" not in by_id["detail"]


def test_conditional_field_hidden_when_its_predicate_is_false():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_COND_SCHEMA, {"kind": "b"})}
    assert "detail" not in by_id
    assert "kind" in by_id


def test_conditional_field_shown_when_its_predicate_is_true():
    by_id = {b["block_id"]: b for b in build_modal_blocks(_COND_SCHEMA, {"kind": "a"})}
    assert "detail" in by_id


# -- A: reacting form controls + the reaction update applied --------------------------------


_REACTIONS: dict[str, Any] = {"field_changed": ["qty"], "page_advanced": [], "submitted": False, "choices": []}


def test_reaction_field_carries_dispatch_action_and_config():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    (block,) = build_modal_blocks(schema, {}, None, None, reactions=_REACTIONS)
    assert block["dispatch_action"] is True
    assert block["element"]["dispatch_action_config"] == {"trigger_actions_on": ["on_enter_pressed"]}


def test_reaction_field_error_renders_a_context_line_after_the_input():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    blocks = build_modal_blocks(schema, {}, None, None, reactions=_REACTIONS, field_errors={"qty": "too big"})
    assert blocks[0]["type"] == "input"
    assert blocks[1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": ":warning: too big"}]}


def test_private_metadata_round_trips_the_accumulated_reaction_state():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    view = build_modal_view(
        "i1",
        "q",
        schema,
        metadata_options={"plan": [{"value": "pro"}]},
        metadata_display={"total": "42"},
    )
    assert decode_private_metadata(view["private_metadata"]) == ("i1", {"plan": [{"value": "pro"}]}, {"total": "42"})


def test_private_metadata_over_cap_is_a_loud_error():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    with pytest.raises(FormSchemaError, match="private_metadata exceeds"):
        build_modal_view("i1", "q", schema, metadata_options={"f": [{"value": "x" * 4000}]})


def test_decode_private_metadata_rejects_malformed():
    with pytest.raises(ValueError, match="private_metadata"):
        decode_private_metadata("")
    with pytest.raises(ValueError, match="not decodable JSON"):
        decode_private_metadata("{not json")
    with pytest.raises(ValueError, match="must be a JSON object"):
        decode_private_metadata("123")
    with pytest.raises(ValueError, match="no interaction id"):
        decode_private_metadata('{"options": {}}')


# -- loud caps and edge branches on the new surfaces -----------------------------------------


def test_array_items_enum_must_be_a_non_empty_list():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": []}}}}
    with pytest.raises(FormSchemaError, match=r"tags.*items enum"):
        build_modal_blocks(schema)


def test_empty_per_send_option_list_is_refused_naming_the_field():
    schema = {"type": "object", "properties": {"tier": {"type": "string"}}}
    with pytest.raises(FormSchemaError, match=r"tier.*non-empty"):
        build_modal_blocks(schema, {}, {"tier": []})


def test_over_long_page_title_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    pages = [{"title": "t" * 151, "fields": ["a"]}]
    with pytest.raises(FormSchemaError, match="page title"):
        build_modal_blocks(schema, {}, {}, pages)


def test_conditional_in_operator_hides_and_shows():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["a", "b", "c"]},
            "detail": {"type": "string", "visibleWhen": {"field": "kind", "in": ["a", "c"]}},
        },
    }
    assert "detail" in {b["block_id"] for b in build_modal_blocks(schema, {"kind": "c"})}
    assert "detail" not in {b["block_id"] for b in build_modal_blocks(schema, {"kind": "b"})}


def test_non_object_property_with_values_still_raises():
    # With values given, the visibility pass walks the properties first (skipping the non-dict
    # one) before the field build refuses it — never a silent skip.
    schema = {"type": "object", "properties": {"bad": "nope"}}
    with pytest.raises(FormSchemaError, match="must be an object"):
        build_modal_blocks(schema, {"x": 1})


def test_review_readback_renders_a_boolean_as_yes_no():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean", "title": "OK"}}}
    pages = [
        {"title": "Fill", "fields": ["ok"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    blocks = build_modal_blocks(schema, {"ok": True}, None, pages)
    assert blocks[-1]["text"]["text"] == "*OK*: Yes"


def test_over_long_review_readback_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"note": {"type": "string", "title": "N"}}}
    pages = [
        {"title": "Fill", "fields": ["note"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    with pytest.raises(FormSchemaError, match="review readback exceeds"):
        build_modal_blocks(schema, {"note": "x" * 3001}, None, pages)


def test_over_long_display_heading_and_body_are_loud_cap_errors():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pytest.raises(FormSchemaError, match="display heading exceeds"):
        build_modal_blocks(
            schema, {}, None, [{"title": "P", "fields": ["a"], "display": [{"kind": "heading", "text": "h" * 151}]}]
        )
    with pytest.raises(FormSchemaError, match="display body exceeds"):
        build_modal_blocks(
            schema, {}, None, [{"title": "P", "fields": ["a"], "display": [{"kind": "body", "text": "b" * 3001}]}]
        )


def test_over_long_reaction_error_message_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pytest.raises(FormSchemaError, match="error message exceeds"):
        build_modal_blocks(schema, {}, None, None, field_errors={"a": "e" * 3001})


def test_image_display_with_no_source_and_no_alt_renders_nothing():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    pages = [{"title": "P", "fields": ["a"], "display": [{"kind": "image", "slot": "pic"}]}]
    # No static src, no slot value, no alt -> nothing to draw, so the block is skipped.
    assert [b["type"] for b in build_modal_blocks(schema, {}, None, pages)] == ["header", "input"]


def test_validate_form_schema_refuses_an_over_cap_modal():
    props = {f"f{i}": {"type": "string"} for i in range(100)}
    with pytest.raises(FormSchemaError, match="modal exceeds 100 blocks"):
        validate_form_schema({"type": "object", "properties": props}, "q")


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "multi_static_select", "selected_options": "not-a-list"},
        {"type": "plain_text_input", "value": 123},
        {"type": "checkboxes"},
        {"type": "datepicker", "selected_date": "2026-01-01"},
    ],
)
def test_array_value_tolerates_odd_entries(entry):
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    state = {"tags": {FIELD_ACTION_ID: entry}}
    # An unreadable/odd array entry yields no list -> the field is omitted, never guessed at.
    assert extract_answer(schema, state) == {}
