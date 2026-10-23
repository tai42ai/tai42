"""Schema → Block Kit mapping: the message button, the modal view, the coerced
answer extraction, and every loud unmappable/cap failure."""

from __future__ import annotations

import json
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
    metadata = decode_private_metadata(view["private_metadata"])
    assert (metadata.interaction_id, metadata.options, metadata.display, metadata.unavailable) == (
        "int-42",
        {},
        {},
        None,
    )
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


_SELECT_SCHEMA = {"type": "object", "properties": {"colour": {"type": "string"}}}
_SELECT_OPTIONS = {"colour": [{"value": f"c{i}"} for i in range(RADIO_OPTION_THRESHOLD + 1)]}


def test_field_choice_label_draws_a_static_select_placeholder():
    (block,) = build_modal_blocks(
        _SELECT_SCHEMA, {}, _SELECT_OPTIONS, field_wording={"colour": {"choice_label": "Pick a colour"}}
    )
    assert block["element"]["type"] == "static_select"
    assert block["element"]["placeholder"] == {"type": "plain_text", "text": "Pick a colour"}


def test_field_without_a_choice_label_draws_no_placeholder():
    (block,) = build_modal_blocks(_SELECT_SCHEMA, {}, _SELECT_OPTIONS)
    assert "placeholder" not in block["element"]


def test_multi_static_select_draws_the_choice_label_placeholder():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    options = {"tags": [{"value": f"t{i}"} for i in range(RADIO_OPTION_THRESHOLD + 1)]}
    (block,) = build_modal_blocks(schema, {}, options, field_wording={"tags": {"choice_label": "Choose tags"}})
    assert block["element"]["type"] == "multi_static_select"
    assert block["element"]["placeholder"] == {"type": "plain_text", "text": "Choose tags"}


def test_radio_buttons_draw_no_placeholder_even_with_a_choice_label():
    # A short list renders as radio_buttons, which has no placeholder slot — the label draws none.
    options = {"colour": [{"value": "r"}, {"value": "b"}]}
    (block,) = build_modal_blocks(
        _SELECT_SCHEMA, {}, options, field_wording={"colour": {"choice_label": "Pick a colour"}}
    )
    assert block["element"]["type"] == "radio_buttons"
    assert "placeholder" not in block["element"]


def test_checkboxes_draw_no_placeholder_even_with_a_choice_label():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    options = {"tags": [{"value": "a"}, {"value": "b"}]}
    (block,) = build_modal_blocks(schema, {}, options, field_wording={"tags": {"choice_label": "Choose tags"}})
    assert block["element"]["type"] == "checkboxes"
    assert "placeholder" not in block["element"]


def test_field_choice_label_over_150_characters_is_refused():
    with pytest.raises(FormSchemaError, match=r"placeholder|150"):
        build_modal_blocks(_SELECT_SCHEMA, {}, _SELECT_OPTIONS, field_wording={"colour": {"choice_label": "x" * 151}})


def test_field_choice_label_of_exactly_150_characters_is_accepted():
    (block,) = build_modal_blocks(
        _SELECT_SCHEMA, {}, _SELECT_OPTIONS, field_wording={"colour": {"choice_label": "x" * 150}}
    )
    assert block["element"]["placeholder"] == {"type": "plain_text", "text": "x" * 150}


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


_INPUT_MODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contact": {"type": "string", "inputMode": "email", "title": "Contact"},
        "site": {"type": "string", "inputMode": "url", "title": "Site"},
        "code": {"type": "string", "inputMode": "numeric"},
        "amount": {"type": "string", "inputMode": "decimal"},
        "line": {"type": "string", "inputMode": "tel"},
        "note": {"type": "string", "inputMode": "text"},
    },
}


def test_email_input_mode_renders_an_email_text_input():
    element = build_modal_blocks(_INPUT_MODE_SCHEMA)[0]["element"]
    assert element == {"type": "email_text_input", "action_id": FIELD_ACTION_ID}


def test_url_input_mode_renders_a_url_text_input():
    element = build_modal_blocks(_INPUT_MODE_SCHEMA)[1]["element"]
    assert element == {"type": "url_text_input", "action_id": FIELD_ACTION_ID}


@pytest.mark.parametrize("field", ["code", "amount", "line", "note"])
def test_numeric_decimal_tel_and_text_input_modes_render_a_plain_text_input(field):
    # Slack carries no numeric/phone keyboard hint on a text element, so numeric/decimal/tel
    # degrade to the plain text input (documented); text is the native mapping. All keep the
    # value exactly as typed.
    by_id = {b["block_id"]: b for b in build_modal_blocks(_INPUT_MODE_SCHEMA)}
    assert by_id[field]["element"] == {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}


def test_email_and_url_input_modes_prefill_their_initial_value():
    values = {"contact": "alice@example.com", "site": "https://example.com"}
    by_id = {b["block_id"]: b for b in build_modal_blocks(_INPUT_MODE_SCHEMA, values)}
    assert by_id["contact"]["element"]["initial_value"] == "alice@example.com"
    assert by_id["site"]["element"]["initial_value"] == "https://example.com"


@pytest.mark.parametrize(
    ("name", "kind", "submitted"),
    [
        ("contact", "email_text_input", "alice@example.com"),
        ("site", "url_text_input", "https://example.com"),
    ],
)
def test_extract_answer_reads_back_email_and_url_text_inputs(name, kind, submitted):
    # The round-trip: a value submitted in Slack's own email/url text element must be read
    # back, not silently dropped. Without _raw_value admitting these element types the value
    # reads as None and the field vanishes from the answer.
    state = {name: {FIELD_ACTION_ID: {"type": kind, "value": submitted}}}
    assert extract_answer(_INPUT_MODE_SCHEMA, state) == {name: submitted}


@pytest.mark.parametrize(("kind", "name"), [("email_text_input", "contact"), ("url_text_input", "site")])
def test_extract_answer_omits_an_empty_email_or_url_text_input(kind, name):
    state = {name: {FIELD_ACTION_ID: {"type": kind, "value": ""}}}
    assert extract_answer(_INPUT_MODE_SCHEMA, state) == {}


def test_unsupported_input_mode_is_refused_naming_the_property():
    schema = {"type": "object", "properties": {"contact": {"type": "string", "inputMode": "voice"}}}
    with pytest.raises(FormSchemaError, match=r"contact.*inputMode"):
        build_modal_blocks(schema)


def test_email_input_mode_trigger_field_carries_dispatch_action_and_config():
    # An email field named as a reaction trigger dispatches like any other text input: the
    # block flags dispatch_action and the element carries the on-enter dispatch config.
    schema = {"type": "object", "properties": {"contact": {"type": "string", "inputMode": "email"}}}
    reactions = {"field_changed": ["contact"], "page_advanced": [], "submitted": False, "choices": []}
    (block,) = build_modal_blocks(schema, {}, None, None, reactions=reactions)
    assert block["dispatch_action"] is True
    assert block["element"]["dispatch_action_config"] == {"trigger_actions_on": ["on_enter_pressed"]}


# -- the single-line text input draws its declared length bounds ------------------


def test_single_line_string_draws_its_declared_max_and_min_length():
    # A plain single-line string draws maxLength/minLength as Slack's max_length/min_length,
    # the same bounds the multi-line textarea draws — on the ordinary plain_text_input.
    schema = {"type": "object", "properties": {"full_name": {"type": "string", "maxLength": 80, "minLength": 2}}}
    element = build_modal_blocks(schema)[0]["element"]
    assert element["type"] == "plain_text_input"
    assert "multiline" not in element
    assert element["max_length"] == 80
    assert element["min_length"] == 2


def test_single_line_string_max_length_above_slacks_3000_cap_is_refused_naming_the_property():
    # The same loud refusal the multi-line textarea path raises: Slack caps
    # plain_text_input.max_length at 3000, so a larger declared maxLength cannot be drawn and
    # is refused at build, never silently truncated.
    schema = {"type": "object", "properties": {"full_name": {"type": "string", "maxLength": 3001}}}
    with pytest.raises(FormSchemaError, match=r"full_name.*3000"):
        build_modal_blocks(schema)


@pytest.mark.parametrize("mode", ["numeric", "decimal", "tel", "text"])
def test_degraded_text_input_modes_draw_their_length_bounds(mode):
    # numeric/decimal/tel/text all draw a plain_text_input, which carries Slack's length bounds.
    schema = {"type": "object", "properties": {"code": {"type": "string", "inputMode": mode, "maxLength": 20}}}
    element = build_modal_blocks(schema)[0]["element"]
    assert element["type"] == "plain_text_input"
    assert element["max_length"] == 20


@pytest.mark.parametrize("mode", ["email", "url"])
def test_email_and_url_inputs_carry_no_slack_length_bound(mode):
    # Slack's email_text_input/url_text_input accept no max_length/min_length, so a declared
    # bound is not drawn on them — it stays enforced at the answer door, never silently dropped.
    schema = {"type": "object", "properties": {"contact": {"type": "string", "inputMode": mode, "maxLength": 50}}}
    element = build_modal_blocks(schema)[0]["element"]
    assert "max_length" not in element
    assert "min_length" not in element


# -- control: chips → the default choice control (the named Slack degrade) --------


def test_chips_on_a_short_string_enum_degrades_to_a_radio_group():
    # Slack has no chip input, so control:chips on a single-choice string draws the default
    # choice control — a radio group at or below the threshold (the documented degrade).
    schema = {
        "type": "object",
        "properties": {"tier": {"type": "string", "enum": ["gold", "silver"], "control": "chips"}},
    }
    element = build_modal_blocks(schema)[0]["element"]
    assert element["type"] == "radio_buttons"
    assert [o["value"] for o in element["options"]] == ["gold", "silver"]


def test_chips_on_a_long_string_enum_degrades_to_a_static_select():
    enum = [f"c{i}" for i in range(RADIO_OPTION_THRESHOLD + 1)]
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": enum, "control": "chips"}}}
    element = build_modal_blocks(schema)[0]["element"]
    assert element["type"] == "static_select"


def test_chips_on_a_short_array_enum_degrades_to_a_checkbox_group():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "control": "chips"}},
    }
    element = build_modal_blocks(schema)[0]["element"]
    assert element["type"] == "checkboxes"
    assert [o["value"] for o in element["options"]] == ["a", "b"]


def test_chips_on_a_long_array_enum_degrades_to_a_multi_static_select():
    enum = [f"c{i}" for i in range(RADIO_OPTION_THRESHOLD + 1)]
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": enum}, "control": "chips"}},
    }
    element = build_modal_blocks(schema)[0]["element"]
    assert element["type"] == "multi_static_select"


def test_chips_with_per_send_options_degrades_to_a_labelled_choice_control():
    # A per-send option list on a chips string still degrades to the choice control (its
    # labels shown, values submitted), replacing the schema enum as it does for any choice.
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["x"], "control": "chips"}}}
    options = {"tier": [{"value": "g", "label": "Gold"}]}
    element = build_modal_blocks(schema, None, options)[0]["element"]
    assert element["type"] == "radio_buttons"
    assert element["options"] == [{"text": {"type": "plain_text", "text": "Gold"}, "value": "g"}]


# -- control: textarea → a multi-line text input with its drawn length bounds -----


def test_textarea_renders_a_multiline_plain_text_input():
    schema = {"type": "object", "properties": {"notes": {"type": "string", "control": "textarea"}}}
    element = build_modal_blocks(schema)[0]["element"]
    assert element == {"type": "plain_text_input", "action_id": FIELD_ACTION_ID, "multiline": True}


def test_textarea_draws_its_declared_max_and_min_length():
    schema = {
        "type": "object",
        "properties": {"notes": {"type": "string", "control": "textarea", "maxLength": 500, "minLength": 10}},
    }
    element = build_modal_blocks(schema)[0]["element"]
    assert element["multiline"] is True
    assert element["max_length"] == 500
    assert element["min_length"] == 10


def test_textarea_max_length_above_slacks_3000_cap_is_refused_naming_the_property():
    # Slack caps plain_text_input.max_length at 3000; a larger declared maxLength is a loud
    # refusal at build, never a silent truncation of the author's bound.
    schema = {"type": "object", "properties": {"notes": {"type": "string", "control": "textarea", "maxLength": 3001}}}
    with pytest.raises(FormSchemaError, match=r"notes.*3000"):
        build_modal_blocks(schema)


def test_textarea_with_per_send_options_degrades_to_the_choice_control():
    # A per-send choice list wins over the text area for this send (the documented precedence
    # a format already has) — the field draws the default choice control.
    schema = {"type": "object", "properties": {"notes": {"type": "string", "control": "textarea"}}}
    options = {"notes": [{"value": "yes", "label": "Yes"}]}
    element = build_modal_blocks(schema, None, options)[0]["element"]
    assert element["type"] == "radio_buttons"


def test_unsupported_control_is_refused_naming_the_property():
    schema = {"type": "object", "properties": {"notes": {"type": "string", "control": "slider"}}}
    with pytest.raises(FormSchemaError, match=r"notes.*control"):
        build_modal_blocks(schema)


# -- the modal's submit / close / title, and the message's open button ------------

_PAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"full_name": {"type": "string"}, "tier": {"type": "string"}},
}


def test_submit_reads_the_last_pages_button_label():
    pages = [
        {"title": "You", "fields": ["full_name"], "button_label": "Keep going"},
        {"title": "Done", "fields": ["tier"], "button_label": "Send it"},
    ]
    view = build_modal_view("int", "Q", _PAGE_SCHEMA, None, None, pages)
    assert view["submit"] == {"type": "plain_text", "text": "Send it"}


def test_submit_defaults_when_the_last_page_sets_no_button_label():
    pages = [{"title": "You", "fields": ["full_name", "tier"]}]
    view = build_modal_view("int", "Q", _PAGE_SCHEMA, None, None, pages)
    assert view["submit"] == {"type": "plain_text", "text": "Submit"}


def test_submit_defaults_without_pages():
    view = build_modal_view("int", "Q", _PAGE_SCHEMA)
    assert view["submit"] == {"type": "plain_text", "text": "Submit"}


def test_submit_label_over_24_characters_is_refused():
    pages = [{"title": "Done", "fields": ["full_name", "tier"], "button_label": "x" * 25}]
    with pytest.raises(FormSchemaError, match=r"submit|24"):
        build_modal_view("int", "Q", _PAGE_SCHEMA, None, None, pages)


def test_close_reads_the_cancel_label_else_the_default():
    view = build_modal_view("int", "Q", _PAGE_SCHEMA, form_wording={"cancel": "Not now"})
    assert view["close"] == {"type": "plain_text", "text": "Not now"}
    assert build_modal_view("int", "Q", _PAGE_SCHEMA)["close"] == {"type": "plain_text", "text": "Cancel"}


def test_cancel_label_over_24_characters_is_refused():
    with pytest.raises(FormSchemaError, match=r"cancel|close|24"):
        build_modal_view("int", "Q", _PAGE_SCHEMA, form_wording={"cancel": "x" * 25})


def test_title_reads_the_form_title_else_the_default():
    view = build_modal_view("int", "Q", _PAGE_SCHEMA, form_wording={"title": "Tell us about you"})
    assert view["title"] == {"type": "plain_text", "text": "Tell us about you"}
    assert build_modal_view("int", "Q", _PAGE_SCHEMA)["title"] == {"type": "plain_text", "text": "Please respond"}


def test_form_title_over_24_characters_is_refused():
    with pytest.raises(FormSchemaError, match=r"title|24"):
        build_modal_view("int", "Q", _PAGE_SCHEMA, form_wording={"title": "x" * 25})


def test_submit_reads_the_form_submit_on_a_page_less_form():
    # A form with NO pages has no page label, so the modal's submit takes the caller's
    # ``wording.form.submit`` when set, the default otherwise.
    view = build_modal_view("int", "Q", _PAGE_SCHEMA, form_wording={"submit": "Send it"})
    assert view["submit"] == {"type": "plain_text", "text": "Send it"}


def test_page_less_form_submit_over_24_characters_is_refused():
    with pytest.raises(FormSchemaError, match=r"submit|24"):
        build_modal_view("int", "Q", _PAGE_SCHEMA, form_wording={"submit": "x" * 25})


def test_paged_form_submit_takes_the_last_page_label_not_the_form_submit():
    # With pages present the terminal button is the LAST page's button_label; a page-less
    # ``form.submit`` is refused beside pages at the ask door, so it never reaches here, and
    # the page label wins regardless.
    pages = [{"title": "Done", "fields": ["full_name", "tier"], "button_label": "Finish"}]
    view = build_modal_view("int", "Q", _PAGE_SCHEMA, None, None, pages, form_wording={"submit": "ignored"})
    assert view["submit"] == {"type": "plain_text", "text": "Finish"}


def test_open_button_reads_the_open_label_else_the_default():
    _section, actions = build_message_blocks("Q", "int-1", open_label="Start here")
    assert actions["elements"][0]["text"] == {"type": "plain_text", "text": "Start here"}
    default = build_message_blocks("Q", "int-1")
    assert default[1]["elements"][0]["text"] == {"type": "plain_text", "text": "Fill form"}


def test_open_label_over_75_characters_is_refused():
    with pytest.raises(FormSchemaError, match=r"open|75"):
        build_message_blocks("Q", "int-1", open_label="x" * 76)


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


def test_extract_answer_uncoercible_integer_is_forwarded_raw():
    # A string that is no integer literal cannot be lifted to ``integer``; it is
    # forwarded as submitted so the answer door rejects it against the schema.
    state = _state()
    state["count"][FIELD_ACTION_ID]["value"] = "not-a-number"
    assert extract_answer(_SCHEMA, state)["count"] == "not-a-number"


def test_extract_answer_uncoercible_number_is_forwarded_raw():
    # A non-numeric string cannot be lifted to ``number``; it is forwarded as
    # submitted so the answer door rejects it against the schema.
    state = _state()
    state["ratio"][FIELD_ACTION_ID]["value"] = "abc"
    assert extract_answer(_SCHEMA, state)["ratio"] == "abc"


@pytest.mark.parametrize("literal", ["1e999", "nan"])
def test_extract_answer_non_finite_number_is_forwarded_raw(literal):
    # A non-finite float passes jsonschema's type:number and serializes to null;
    # the string does not pass, so it is forwarded for the answer door to reject.
    state = _state()
    state["ratio"][FIELD_ACTION_ID]["value"] = literal
    assert extract_answer(_SCHEMA, state)["ratio"] == literal


def test_extract_answer_unrecognized_boolean_is_forwarded_raw():
    # A string that is neither "true" nor "false" cannot be lifted to ``boolean``;
    # it is forwarded as submitted so the answer door rejects it against the schema.
    state = _state()
    state["subscribed"][FIELD_ACTION_ID]["selected_option"]["value"] = "maybe"
    assert extract_answer(_SCHEMA, state)["subscribed"] == "maybe"


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


def test_review_readback_empty_line_reads_the_caller_review_empty():
    # The caller's ``wording.form.review_empty`` replaces the platform's empty-readback line.
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    pages = [
        {"title": "Fill", "fields": ["name"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    blocks = build_modal_blocks(schema, {}, None, pages, review_empty="Nothing filled in yet.")
    assert blocks[-1]["text"]["text"] == "Nothing filled in yet."


def test_build_modal_view_threads_review_empty_from_form_wording():
    # The review line rides through build_modal_view's form_wording so the click-time open and
    # every re-render draw the caller's line, not the default.
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    pages = [
        {"title": "Fill", "fields": ["name"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    view = build_modal_view("int", "Q", schema, {}, None, pages, form_wording={"review_empty": "Nothing yet."})
    assert view["blocks"][-1]["text"]["text"] == "Nothing yet."


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
    metadata = decode_private_metadata(view["private_metadata"])
    assert metadata.interaction_id == "i1"
    assert metadata.options == {"plan": [{"value": "pro"}]}
    assert metadata.display == {"total": "42"}
    assert metadata.unavailable is None


def test_private_metadata_round_trips_the_caller_unavailable_line():
    # The caller's ``status.unavailable`` rides the view's own carrier, so the submit door can draw
    # it after the form record is gone.
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    view = build_modal_view("i1", "q", schema, status_unavailable="This form is closed now.")
    metadata = decode_private_metadata(view["private_metadata"])
    assert metadata.interaction_id == "i1"
    assert metadata.unavailable == "This form is closed now."


def test_private_metadata_without_an_unavailable_line_writes_no_key():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    view = build_modal_view("i1", "q", schema)
    assert "unavailable" not in json.loads(view["private_metadata"])


def test_private_metadata_unavailable_line_over_the_cap_is_a_loud_error():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    with pytest.raises(FormSchemaError, match="private_metadata exceeds"):
        build_modal_view("i1", "q", schema, status_unavailable="x" * 3000)


@pytest.mark.parametrize("raw", ['{"id": "i1", "unavailable": 5}', '{"id": "i1", "unavailable": "  "}'])
def test_decode_private_metadata_rejects_a_malformed_unavailable_line(raw):
    with pytest.raises(ValueError, match="unavailable"):
        decode_private_metadata(raw)


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


def test_review_readback_renders_a_boolean_in_the_caller_words():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean", "title": "OK"}, "late": {"type": "boolean", "title": "Late"}},
    }
    pages = [
        {"title": "Fill", "fields": ["ok", "late"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    view = build_modal_view(
        "i1",
        "q",
        schema,
        {"ok": True, "late": False},
        None,
        pages,
        form_wording={"fields": {"ok": {"true_label": "Agreed"}, "late": {"false_label": "On time"}}},
    )
    assert view["blocks"][-1]["text"]["text"] == "*OK*: Agreed\n*Late*: On time"


def test_boolean_radio_draws_the_caller_true_and_false_labels():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    (block,) = build_modal_blocks(schema, field_wording={"ok": {"true_label": "Agreed", "false_label": "Declined"}})
    assert block["element"]["type"] == "radio_buttons"
    assert [(o["text"]["text"], o["value"]) for o in block["element"]["options"]] == [
        ("Agreed", "true"),
        ("Declined", "false"),
    ]


def test_boolean_radio_fills_an_unset_side_with_the_default_word():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    (block,) = build_modal_blocks(schema, field_wording={"ok": {"false_label": "Declined"}})
    assert [o["text"]["text"] for o in block["element"]["options"]] == ["Yes", "Declined"]


@pytest.mark.parametrize("member", ["true_label", "false_label"])
def test_boolean_radio_label_over_75_is_a_loud_cap_error(member):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    with pytest.raises(FormSchemaError, match=rf"'ok'.*{member}.*75"):
        build_modal_blocks(schema, field_wording={"ok": {member: "x" * 76}})
    (block,) = build_modal_blocks(schema, field_wording={"ok": {member: "x" * 75}})
    assert "x" * 75 in [o["text"]["text"] for o in block["element"]["options"]]


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


def test_option_description_renders_as_the_second_line_on_a_radio_group():
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}}
    options = {"plan": [{"value": "s", "label": "Standard", "description": "2-3 days"}]}
    (block,) = build_modal_blocks(schema, {}, options)

    element = block["element"]
    # A short per-send list is a radio group; the option carries its second line verbatim.
    assert element["type"] == "radio_buttons"
    assert element["options"] == [
        {
            "text": {"type": "plain_text", "text": "Standard"},
            "value": "s",
            "description": {"type": "plain_text", "text": "2-3 days"},
        }
    ]


def test_option_description_renders_on_a_static_select_above_the_threshold():
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}}
    options = {
        "plan": [
            {"value": f"p{i}", "label": f"Plan {i}", "description": f"desc {i}"}
            for i in range(RADIO_OPTION_THRESHOLD + 1)
        ]
    }
    (block,) = build_modal_blocks(schema, {}, options)

    element = block["element"]
    assert element["type"] == "static_select"
    assert element["options"][0]["description"] == {"type": "plain_text", "text": "desc 0"}


def test_option_description_renders_on_a_checkboxes_group():
    schema = {"type": "object", "properties": {"plans": {"type": "array", "items": {"type": "string"}}}}
    options = {"plans": [{"value": "a", "label": "Alpha", "description": "first"}]}
    (block,) = build_modal_blocks(schema, {}, options)

    element = block["element"]
    assert element["type"] == "checkboxes"
    assert element["options"][0]["description"] == {"type": "plain_text", "text": "first"}


def test_option_description_renders_on_a_multi_static_select_above_the_threshold():
    schema = {"type": "object", "properties": {"plans": {"type": "array", "items": {"type": "string"}}}}
    options = {
        "plans": [
            {"value": f"a{i}", "label": f"Alpha {i}", "description": f"d{i}"} for i in range(RADIO_OPTION_THRESHOLD + 1)
        ]
    }
    (block,) = build_modal_blocks(schema, {}, options)

    element = block["element"]
    assert element["type"] == "multi_static_select"
    assert element["options"][0]["description"] == {"type": "plain_text", "text": "d0"}


def test_option_without_a_description_carries_no_description_key():
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}}
    (block,) = build_modal_blocks(schema, {}, {"plan": [{"value": "s", "label": "Standard"}]})
    assert "description" not in block["element"]["options"][0]


def test_over_long_option_description_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"plan": {"type": "string"}}}
    options = {"plan": [{"value": "s", "label": "Standard", "description": "d" * 76}]}
    with pytest.raises(FormSchemaError, match="description exceeds 75 characters"):
        build_modal_blocks(schema, {}, options)


def test_field_description_renders_as_the_input_block_hint():
    schema = {"type": "object", "properties": {"amount": {"type": "string", "description": "in whole units"}}}
    (block,) = build_modal_blocks(schema)
    assert block["hint"] == {"type": "plain_text", "text": "in whole units"}


def test_blank_field_description_draws_no_hint():
    schema = {"type": "object", "properties": {"amount": {"type": "string", "description": "   "}}}
    (block,) = build_modal_blocks(schema)
    assert "hint" not in block


def test_over_long_field_description_is_a_loud_cap_error():
    schema = {"type": "object", "properties": {"amount": {"type": "string", "description": "d" * 2001}}}
    with pytest.raises(FormSchemaError, match="hint exceeds"):
        build_modal_blocks(schema)


def test_validate_form_schema_refuses_an_over_cap_field_description():
    schema = {"type": "object", "properties": {"amount": {"type": "string", "description": "d" * 2001}}}
    with pytest.raises(FormSchemaError, match="hint exceeds"):
        validate_form_schema(schema, "q")


def test_number_bounds_render_as_the_number_inputs_min_and_max():
    schema = {
        "type": "object",
        "properties": {
            "qty": {"type": "integer", "minimum": 1, "maximum": 9},
            "rate": {"type": "number", "minimum": 0.5},
            "free": {"type": "number", "exclusiveMinimum": 0},
        },
    }
    by_id = {block["block_id"]: block["element"] for block in build_modal_blocks(schema)}
    assert by_id["qty"] == {
        "type": "number_input",
        "action_id": FIELD_ACTION_ID,
        "is_decimal_allowed": False,
        "min_value": "1",
        "max_value": "9",
    }
    assert by_id["rate"]["min_value"] == "0.5"
    assert "max_value" not in by_id["rate"]
    # An exclusive bound has no Slack element: the answer door refuses it, pinned under the field.
    assert "min_value" not in by_id["free"]


def test_an_integer_fields_bounds_are_the_tightest_whole_numbers():
    # An integer input admits no decimal, so its bounds are whole numbers: a fractional bound
    # rounds inward (the same whole numbers pass) and an integral float reads without ``.0``.
    schema = {
        "type": "object",
        "properties": {
            "qty": {"type": "integer", "minimum": 1.5, "maximum": 9.5},
            "whole": {"type": "integer", "minimum": 1.0, "maximum": 3.0},
            "low": {"type": "integer", "minimum": -2.5},
        },
    }
    by_id = {block["block_id"]: block["element"] for block in build_modal_blocks(schema)}
    assert (by_id["qty"]["min_value"], by_id["qty"]["max_value"]) == ("2", "9")
    assert (by_id["whole"]["min_value"], by_id["whole"]["max_value"]) == ("1", "3")
    assert by_id["low"]["min_value"] == "-2"


def test_a_number_fields_bounds_read_as_plain_decimals():
    schema = {"type": "object", "properties": {"rate": {"type": "number", "minimum": 1.0, "maximum": 1e20}}}
    element = build_modal_blocks(schema)[0]["element"]
    assert (element["min_value"], element["max_value"]) == ("1", "100000000000000000000")


def test_email_format_renders_an_email_text_input():
    schema = {"type": "object", "properties": {"contact": {"type": "string", "format": "email"}}}
    assert build_modal_blocks(schema)[0]["element"] == {"type": "email_text_input", "action_id": FIELD_ACTION_ID}


# -- a range with a time on each end: each time draws and reads back right after its date ---------

# The times are declared LAST in the schema.
_TIMED_RANGE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date", "title": "Start"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "End"},
        "note": {"type": "string", "title": "Note"},
        "start_time": {"type": "string", "format": "time", "title": "Start time", "timeOf": "start"},
        "end_time": {"type": "string", "format": "time", "title": "End time", "timeOf": "end"},
    },
}


def _drawn(blocks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(block["block_id"], block["element"]["type"]) for block in blocks if block["type"] == "input"]


_TIMED_DRAWN = [
    ("start", "datepicker"),
    ("start_time", "timepicker"),
    ("end", "datepicker"),
    ("end_time", "timepicker"),
]


def test_a_page_less_form_draws_each_timepicker_right_after_its_datepicker():
    assert _drawn(build_modal_blocks(_TIMED_RANGE)) == [*_TIMED_DRAWN, ("note", "plain_text_input")]


def test_a_page_draws_each_timepicker_right_after_its_datepicker():
    pages = [{"title": "When", "fields": ["start", "end", "start_time", "end_time"]}]
    assert _drawn(build_modal_blocks(_TIMED_RANGE, {}, None, pages)) == _TIMED_DRAWN


def test_a_review_readback_lists_each_time_right_after_its_date():
    pages = [
        {"title": "When", "fields": ["start", "end", "start_time", "end_time", "note"], "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]
    values = {"start": "2026-05-01", "end": "2026-05-02", "note": "hi", "start_time": "09:00", "end_time": "10:00"}
    blocks = build_modal_blocks(_TIMED_RANGE, values, None, pages)
    assert blocks[-1]["text"]["text"].split("\n") == [
        "*Start*: 2026-05-01",
        "*Start time*: 09:00",
        "*End*: 2026-05-02",
        "*End time*: 10:00",
        "*Note*: hi",
    ]
