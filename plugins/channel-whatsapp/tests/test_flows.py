"""``flows.build_form_flow`` — the answer-schema → publishable Flow JSON mapping: the
supported type subset, the unsupported-shape rejections, the letters-and-underscores
screen ids, the no-``Form`` control-level prefill, and the publish key."""

from __future__ import annotations

import json
import re

import pytest
from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows import (
    FORM_ENTRY_SCREEN,
    _canonical_hash_pages,
    build_flow_data,
    build_form_flow,
    component_names,
    payload_labels,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE_RE = re.compile(r"\$\{(?:data|form|screen)\.([^}]+)\}")


def _assert_only_identifier_safe_wire_names(flow_json: dict) -> None:
    """Every component ``name``, screen-``data`` key, navigate-``payload`` key and
    ``${data.…}`` / ``${form.…}`` reference in the Flow is in Meta's identifier grammar.

    Two things are deliberately excluded — both may carry any character: the field LABEL
    (it keeps the property title, else the raw property name) and the terminal ``complete``
    action's payload KEYS (they are the human-readable completion labels).
    """
    for reference in _REFERENCE_RE.findall(json.dumps(flow_json)):
        assert _IDENTIFIER_RE.match(reference), reference

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") in {"TextInput", "Dropdown", "OptIn", "CalendarPicker"} and isinstance(
                node.get("name"), str
            ):
                assert _IDENTIFIER_RE.match(node["name"]), node["name"]
            if isinstance(node.get("data"), dict):
                for key in node["data"]:
                    assert _IDENTIFIER_RE.match(key), key
            action = node.get("on-click-action")
            if isinstance(action, dict) and action.get("name") == "navigate":
                for key in action.get("payload", {}):
                    assert _IDENTIFIER_RE.match(key), key
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(flow_json)


def _screen_children(flow_json: dict, index: int = 0) -> list[dict]:
    """A screen's children — the field components then the Footer, read DIRECTLY.

    There is no ``Form`` hop: the controls sit in the ``SingleColumnLayout`` children.
    """
    return flow_json["screens"][index]["layout"]["children"]


def _controls(flow_json: dict, index: int = 0) -> list[dict]:
    """A screen's field components (everything but the Footer)."""
    return [child for child in _screen_children(flow_json, index) if child["type"] != "Footer"]


# -- the single source of truth for the entry screen id ------------------------


def test_form_entry_screen_is_screen_a():
    assert FORM_ENTRY_SCREEN == "SCREEN_A"


# -- one screen per page, letters-only ids, no Form ----------------------------


def test_form_flow_one_page_is_one_dynamic_screen():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    flow_json, _ = build_form_flow(schema)

    assert flow_json["version"] == "7.0"
    assert len(flow_json["screens"]) == 1
    screen = flow_json["screens"][0]
    assert screen["id"] == "SCREEN_A"
    assert screen["terminal"] is True
    assert screen["layout"]["type"] == "SingleColumnLayout"
    field = _controls(flow_json)[0]
    # Every control reads its init-value from the screen data (so a send can prefill it).
    assert field["type"] == "TextInput"
    assert field["init-value"] == "${data.note__init}"
    # A single-screen flow needs no routing model.
    assert "routing_model" not in flow_json


def test_form_flow_one_screen_per_page():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [{"title": "First", "fields": ["a"]}, {"title": "Second", "fields": ["b"]}]

    flow_json, _ = build_form_flow(schema, pages)

    assert [s["id"] for s in flow_json["screens"]] == ["SCREEN_A", "SCREEN_B"]
    assert [s["title"] for s in flow_json["screens"]] == ["First", "Second"]
    assert flow_json["screens"][0]["terminal"] is False
    assert flow_json["screens"][1]["terminal"] is True
    assert flow_json["routing_model"] == {"SCREEN_A": ["SCREEN_B"], "SCREEN_B": []}
    # The terminal screen completes with the flat union of every field: this screen's
    # value through the form-input reference, an earlier one from its __val carrier.
    footer = _screen_children(flow_json, 1)[-1]
    assert footer["on-click-action"]["name"] == "complete"
    assert footer["on-click-action"]["payload"] == {"a": "${data.a__val}", "b": "${form.b}"}
    # The first step navigates forward, carrying its collected value on as ${form.<field>}.
    step_footer = _screen_children(flow_json, 0)[-1]
    assert step_footer["on-click-action"]["name"] == "navigate"
    assert step_footer["on-click-action"]["next"] == {"type": "screen", "name": "SCREEN_B"}
    assert step_footer["on-click-action"]["payload"]["a__val"] == "${form.a}"


# -- the field-component mapping (each keeps its init-value) --------------------


def test_string_maps_to_text_input_with_title_label():
    schema = {"type": "object", "properties": {"note": {"type": "string", "title": "Your note"}}, "required": []}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "TextInput",
        "name": "note",
        "label": "Your note",
        "required": False,
        "init-value": "${data.note__init}",
        "error-message": "${data.note__err}",
    }


def test_label_falls_back_to_property_name():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "TextInput",
        "name": "note",
        "label": "note",
        "required": True,
        "init-value": "${data.note__init}",
        "error-message": "${data.note__err}",
    }


@pytest.mark.parametrize("title", ["", "   ", "\t\n", 123, None, ["x"]])
def test_blank_or_non_string_title_falls_back_to_property_name(title: object):
    # A title that is not a string, or is empty / whitespace-only, counts as absent — WhatsApp
    # cannot show a blank field label or completion-payload key — so BOTH the rendered control
    # label and the completion-payload key fall back to the property name.
    schema = {"type": "object", "properties": {"note": {"type": "string", "title": title}}, "required": []}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field["label"] == "note"
    assert payload_labels(schema["properties"]) == {"note": "note"}


def test_short_string_enum_maps_to_radio_group():
    # An enum at or below the radio threshold (5) renders as a RadioButtonsGroup — the short-list
    # control — still reading a dynamic data-source so a per-send option list can replace it.
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": ["a", "b", "c"]}},
        "required": ["pick"],
    }

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "RadioButtonsGroup",
        "name": "pick",
        "label": "pick",
        "required": True,
        "data-source": "${data.pick__ds}",
        "init-value": "${data.pick__init}",
        "error-message": "${data.pick__err}",
    }


def test_long_string_enum_maps_to_dropdown():
    # An enum above the radio threshold stays a Dropdown.
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": ["a", "b", "c", "d", "e", "f"]}},
        "required": ["pick"],
    }

    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "Dropdown"
    assert field["data-source"] == "${data.pick__ds}"


def test_option_bearing_string_without_enum_stays_dropdown():
    # A string marked option-bearing by the ask (per-send options, no schema enum) has no fixed
    # option count at publish, so it stays the dynamic Dropdown (never a radio group).
    schema = {"type": "object", "properties": {"pick": {"type": "string"}}, "required": ["pick"]}

    field = _controls(build_form_flow(schema, option_fields={"pick"})[0])[0]
    assert field["type"] == "Dropdown"
    assert field["data-source"] == "${data.pick__ds}"


def test_boolean_maps_to_optin_with_init_value():
    schema = {"type": "object", "properties": {"agree": {"type": "boolean"}}, "required": []}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "OptIn",
        "name": "agree",
        "label": "agree",
        "required": False,
        "init-value": "${data.agree__init}",
    }


@pytest.mark.parametrize("json_type", ["integer", "number"])
def test_integer_and_number_map_to_number_text_input(json_type: str):
    schema = {"type": "object", "properties": {"qty": {"type": json_type}}, "required": ["qty"]}

    field = _controls(build_form_flow(schema)[0])[0]
    # An integer also draws the whole-number shape as a native pattern, whose helper text Meta requires.
    whole = {"pattern": "^-?[0-9]+$", "helper-text": "A whole number, like 42"} if json_type == "integer" else {}
    assert field == {
        "type": "TextInput",
        "name": "qty",
        "label": "qty",
        "required": True,
        "input-type": "number",
        "init-value": "${data.qty__init}",
        **whole,
        "error-message": "${data.qty__err}",
    }


# -- the string inputMode → TextInput keyboard mapping -------------------------


@pytest.mark.parametrize(
    ("mode", "input_type"),
    [("numeric", "number"), ("decimal", "number"), ("tel", "phone"), ("email", "email")],
)
def test_string_input_mode_draws_the_matching_keyboard(mode: str, input_type: str):
    # A string field's ``inputMode`` selects Meta's ``input-type`` keyboard: a numeric keypad for
    # ``numeric``/``decimal`` (its keypad admits a decimal separator), the phone keypad for ``tel``,
    # the email keyboard for ``email``. The value still arrives as the typed string.
    schema = {"type": "object", "properties": {"field": {"type": "string", "inputMode": mode}}, "required": ["field"]}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "TextInput",
        "name": "field",
        "label": "field",
        "required": True,
        "input-type": input_type,
        "init-value": "${data.field__init}",
        "error-message": "${data.field__err}",
    }


@pytest.mark.parametrize("mode", ["url", "text"])
def test_string_input_mode_url_and_text_stay_a_plain_text_input(mode: str):
    # ``text`` is the identity (no keyboard hint); ``url`` degrades to the plain text field because
    # Meta's TextInput has no ``url`` input-type. Both emit the plain TextInput, byte-identical to a
    # string field with no ``inputMode`` at all.
    schema = {"type": "object", "properties": {"field": {"type": "string", "inputMode": mode}}, "required": ["field"]}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "TextInput",
        "name": "field",
        "label": "field",
        "required": True,
        "init-value": "${data.field__init}",
        "error-message": "${data.field__err}",
    }


def test_string_input_mode_text_is_byte_identical_to_absent():
    plain = {"type": "object", "properties": {"field": {"type": "string"}}, "required": ["field"]}
    with_text = {
        "type": "object",
        "properties": {"field": {"type": "string", "inputMode": "text"}},
        "required": ["field"],
    }

    assert _controls(build_form_flow(plain)[0])[0] == _controls(build_form_flow(with_text)[0])[0]


def test_string_bogus_input_mode_raises_channel_input_error_naming_the_property():
    schema = {
        "type": "object",
        "properties": {"field": {"type": "string", "inputMode": "bogus"}},
        "required": ["field"],
    }

    with pytest.raises(ChannelInputError, match="'field'"):
        build_form_flow(schema)


def test_bogus_input_mode_surfaces_as_value_error_at_ask_time():
    # The ask-time hook turns the builder's ``ChannelInputError`` into the ``ValueError`` the
    # chokepoint expects, so an out-of-set inputMode is refused before any state is written.
    from tai42_channel_whatsapp import WhatsAppChannel

    schema = {
        "type": "object",
        "properties": {"field": {"type": "string", "inputMode": "bogus"}},
        "required": ["field"],
    }

    with pytest.raises(ValueError, match="'field'"):
        WhatsAppChannel().validate_form_schema(schema, "question")


# -- control: chips → ChipsSelector -------------------------------------------


def test_string_enum_chips_renders_a_single_pick_chips_selector():
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": ["a", "b", "c"], "control": "chips"}},
        "required": ["pick"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "ChipsSelector",
        "name": "pick",
        "label": "pick",
        "required": True,
        "data-source": "${data.pick__ds}",
        "init-value": "${data.pick__init}",
        "max-selected-items": 1,
    }
    # The single-pick control's init is an array-typed carrier (a one-element list on the wire).
    assert build_flow_data(schema, {"pick": "b"}, {}) == {
        "pick__init": ["b"],
        "pick__ds": [{"id": "a", "title": "a"}, {"id": "b", "title": "b"}, {"id": "c", "title": "c"}],
    }
    assert build_flow_data(schema, {}, {})["pick__init"] == []


def test_array_chips_renders_a_multi_pick_chips_selector_with_selection_bounds():
    schema = {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {"type": "string", "enum": ["a", "b", "c"]},
                "control": "chips",
                "minItems": 1,
                "maxItems": 2,
            }
        },
        "required": ["picks"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "ChipsSelector"
    assert field["min-selected-items"] == 1
    assert field["max-selected-items"] == 2
    # A multi-pick answer is a list and needs no unwrap; its init stays the list.
    assert build_flow_data(schema, {"picks": ["a", "b"]}, {})["picks__init"] == ["a", "b"]


def test_array_chips_without_max_items_defaults_the_vendor_ceiling_and_a_required_floor():
    schema = {
        "type": "object",
        "properties": {"picks": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "control": "chips"}},
        "required": ["picks"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["max-selected-items"] == 20  # Meta's ceiling when no maxItems is modeled
    assert field["min-selected-items"] == 1  # a required array with no minItems floor demands one pick


@pytest.mark.parametrize("count", [1, 21])
def test_chips_outside_meta_bounds_degrades_to_the_default_control(count: int):
    # Fewer than 2 or more than 20 choices cannot render as a ChipsSelector, so the field DEGRADES to
    # the shape's default control (a named, documented degrade) — a radio/dropdown here.
    enum = [f"opt{n}" for n in range(count)]
    kind = "RadioButtonsGroup" if count <= 5 else "Dropdown"
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": enum, "control": "chips"}},
        "required": ["pick"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == kind
    # The degraded control's value is the scalar string, so its init is the empty string, not a list.
    assert build_flow_data(schema, {}, {})["pick__init"] == ""


# -- control: textarea / drawn maxLength / minLength --------------------------


def test_string_textarea_control_renders_a_text_area_with_max_length():
    schema = {
        "type": "object",
        "properties": {"notes": {"type": "string", "control": "textarea", "maxLength": 500}},
        "required": ["notes"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "TextArea",
        "name": "notes",
        "label": "notes",
        "required": True,
        "max-length": 500,
        "init-value": "${data.notes__init}",
        "error-message": "${data.notes__err}",
    }


def test_string_textarea_without_max_length_omits_it():
    schema = {
        "type": "object",
        "properties": {"notes": {"type": "string", "control": "textarea"}},
        "required": ["notes"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "TextArea"
    assert "max-length" not in field


def test_single_line_text_input_draws_max_chars_and_min_chars_from_length_bounds():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "maxLength": 80, "minLength": 2}},
        "required": ["name"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "TextInput"
    assert field["max-chars"] == 80
    assert field["min-chars"] == 2


# -- the format: date → CalendarPicker mapping --------------------------------


def test_string_with_format_date_maps_to_a_single_calendar_picker_carrying_required():
    # Every date draws a single-mode CalendarPicker: unlike the DatePicker it carries ``required``.
    schema = {
        "type": "object",
        "properties": {"when": {"type": "string", "format": "date", "title": "Pick a date"}},
        "required": ["when"],
    }

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "CalendarPicker",
        "name": "when",
        "label": "Pick a date",
        "mode": "single",
        "required": True,
        "init-value": "${data.when__init}",
        "error-message": "${data.when__err}",
    }


@pytest.mark.parametrize("fmt", ["time", "date-time"])
def test_string_with_format_time_or_date_time_stays_text_input(fmt: str):
    # The vendor has no time-of-day picker, so these render as a text input carrying the platform's
    # format regex as its native pattern; the data endpoint and the answer door check the calendar.
    schema = {"type": "object", "properties": {"at": {"type": "string", "format": fmt}}, "required": ["at"]}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "TextInput"
    assert field["required"] is True
    assert field["pattern"].startswith("^[0-9]")
    assert field["helper-text"].startswith("A time as HH:MM" if fmt == "time" else "Date, time and zone")
    assert field["error-message"] == "${data.at__err}"


def test_enum_outranks_format_date_and_still_renders_a_choice():
    # An explicit choice list is a stronger instruction than a format hint; a short enum renders
    # as the radio short-list control rather than a date picker.
    schema = {
        "type": "object",
        "properties": {"day": {"type": "string", "format": "date", "enum": ["2026-09-27", "2026-09-28"]}},
        "required": ["day"],
    }

    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "RadioButtonsGroup"


def test_option_bearing_outranks_format_date_and_still_renders_a_dropdown():
    schema = {"type": "object", "properties": {"day": {"type": "string", "format": "date"}}, "required": ["day"]}

    field = _controls(build_form_flow(schema, None, {"day"})[0])[0]
    assert field["type"] == "Dropdown"


def test_valid_date_prefill_rides_as_the_init_string():
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": "date"}}, "required": ["when"]}

    data = build_flow_data(schema, {"when": "2026-09-27"}, {})
    assert data["when__init"] == "2026-09-27"


def test_absent_date_prefill_defaults_to_empty_string():
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": "date"}}, "required": ["when"]}

    data = build_flow_data(schema, {}, {})
    assert data["when__init"] == ""


@pytest.mark.parametrize(
    "bad", ["27/09/2026", "2026-9-7", "2026-13-40", "not-a-date", "20260927", "2026-09-27T00:00", 5]
)
def test_invalid_date_prefill_is_refused_naming_the_property(bad: object):
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": "date"}}, "required": ["when"]}

    with pytest.raises(ChannelInputError, match="when"):
        build_flow_data(schema, {"when": bad}, {})


# -- the vendor-legality invariants (no Form, control-level init-value, letters ids) --


def test_no_screen_carries_a_form_node():
    # The vendor rejects a control-level init-value inside a Form; no screen carries one.
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "boolean"}, "c": {"type": "string", "enum": ["x"]}},
    }
    pages = [{"title": "One", "fields": ["a", "b"]}, {"title": "Two", "fields": ["c"]}]

    flow_json, _ = build_form_flow(schema, pages)

    for screen in flow_json["screens"]:
        assert screen["layout"]["type"] == "SingleColumnLayout"
        assert all(child["type"] != "Form" for child in screen["layout"]["children"])


def test_every_control_carries_its_init_value():
    schema = {
        "type": "object",
        "properties": {
            "s": {"type": "string"},
            "n": {"type": "integer"},
            "flag": {"type": "boolean"},
            "pick": {"type": "string", "enum": ["x", "y"]},
        },
    }

    flow_json, _ = build_form_flow(schema)

    for control in _controls(flow_json):
        assert control["init-value"] == f"${{data.{control['name']}__init}}"


def test_every_screen_id_is_letters_and_underscores_past_nine():
    # Eleven pages exercise a two-digit index (SCREEN_BA at page 10), proving the
    # scheme stays letters-only past nine.
    props = {f"f{i}": {"type": "string"} for i in range(11)}
    pages = [{"title": f"P{i}", "fields": [f"f{i}"]} for i in range(11)]

    flow_json, _ = build_form_flow({"type": "object", "properties": props}, pages)

    ids = [s["id"] for s in flow_json["screens"]]
    assert ids[0] == "SCREEN_A"
    assert ids[9] == "SCREEN_J"
    assert ids[10] == "SCREEN_BA"
    for screen_id in ids:
        assert re.fullmatch(r"[A-Za-z_]+", screen_id)


def test_local_field_references_use_the_form_input_spelling():
    # A value on THIS screen is read as ${form.<field>} (Meta's reference for data the
    # user entered); an earlier screen's value rides forward as ${data.<field>__val}.
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [{"title": "1", "fields": ["a"]}, {"title": "2", "fields": ["b"]}]

    flow_json, _ = build_form_flow(schema, pages)

    step_payload = _screen_children(flow_json, 0)[-1]["on-click-action"]["payload"]
    assert step_payload["a__val"] == "${form.a}"  # this-screen value, form-input spelling
    terminal_payload = _screen_children(flow_json, 1)[-1]["on-click-action"]["payload"]
    assert terminal_payload["b"] == "${form.b}"  # this-screen value on the terminal
    assert terminal_payload["a"] == "${data.a__val}"  # earlier value from its carrier


def test_every_reference_in_the_emitted_flow_is_a_form_or_data_reference():
    # Meta resolves a control's just-filled value through the form-input reference
    # ${form.<name>} and a value passed down (the navigate carrier) through ${data.<name>};
    # every reference the emitted Flow carries is one of those two grammars, so nothing
    # comes back to the sender as a literal, unresolved reference string.
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [{"title": "1", "fields": ["a"]}, {"title": "2", "fields": ["b"]}]

    flow_json, _ = build_form_flow(schema, pages)

    references = re.findall(r"\$\{[^}]+\}", json.dumps(flow_json))
    assert references  # the flow does carry references
    for reference in references:
        assert reference.startswith(("${form.", "${data.")), reference


def test_a_boolean_a_string_and_a_dropdown_are_each_prefilled():
    # build_flow_data fills each control type's __init (boolean as a Python bool) and a
    # dropdown's __ds, so the send injects the prefill the controls read.
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string"},
            "agree": {"type": "boolean"},
            "tier": {"type": "string", "enum": ["gold", "silver"]},
        },
    }

    data = build_flow_data(schema, {"note": "hi", "agree": True}, {})

    assert data["note__init"] == "hi"
    assert data["agree__init"] is True
    assert data["tier__init"] == ""
    assert data["tier__ds"] == [{"id": "gold", "title": "gold"}, {"id": "silver", "title": "silver"}]


# -- the ask-time refusals, through build_form_flow ----------------------------


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param({"type": "array", "items": {"type": "string"}}, id="top-level-array"),
        pytest.param({"type": "object", "properties": {}}, id="empty-properties"),
        pytest.param({"type": "object"}, id="no-properties"),
        pytest.param({"type": "object", "properties": {"x": {"type": "object"}}}, id="nested-object"),
        pytest.param({"type": "object", "properties": {"x": {"type": "array"}}}, id="array-property"),
        pytest.param({"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}]}}}, id="oneOf"),
        pytest.param({"type": "object", "properties": {"x": {"type": "unknown"}}}, id="unknown-type"),
    ],
)
def test_unsupported_schema_raises_naming_the_property(schema: dict):
    with pytest.raises(ChannelInputError):
        build_form_flow(schema)


def test_unsupported_property_error_names_the_property():
    schema = {"type": "object", "properties": {"widget": {"type": "object"}}, "required": []}
    with pytest.raises(ChannelInputError, match="'widget'"):
        build_form_flow(schema)


@pytest.mark.parametrize(
    "enum",
    [pytest.param([], id="empty"), pytest.param([1, 2], id="non-string-member"), pytest.param("ab", id="not-a-list")],
)
def test_string_enum_must_be_non_empty_list_of_strings(enum: object):
    # A string enum must be a non-empty list of strings — the per-send validator refuses
    # anything else, so the builder that actually sends refuses a malformed enum.
    schema = {"type": "object", "properties": {"pick": {"type": "string", "enum": enum}}, "required": []}
    with pytest.raises(ChannelInputError, match="'pick'"):
        build_form_flow(schema)


def test_required_must_be_a_list_of_strings():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": "note"}
    with pytest.raises(ChannelInputError, match="'required'"):
        build_form_flow(schema)


def test_property_value_must_be_an_object():
    schema = {"type": "object", "properties": {"note": "string"}, "required": []}
    with pytest.raises(ChannelInputError, match="'note'"):
        build_form_flow(schema)


def test_reserved_flow_token_property_is_refused():
    # ``flow_token`` is Meta's own key on the Flow response; the reply handler strips
    # it, so a field of that name is unanswerable. The mapper refuses it up front.
    schema = {"type": "object", "properties": {"flow_token": {"type": "string"}}, "required": []}
    with pytest.raises(ChannelInputError, match=r"'flow_token'.*reserved"):
        build_form_flow(schema)


# -- the publish key ------------------------------------------------------------


def test_form_flow_key_differs_when_pages_differ():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    _, key_one_page = build_form_flow(schema)
    _, key_two_pages = build_form_flow(schema, [{"title": "1", "fields": ["a"]}, {"title": "2", "fields": ["b"]}])
    _, key_other_split = build_form_flow(schema, [{"title": "1", "fields": ["a", "b"]}])

    assert key_one_page != key_two_pages
    assert key_two_pages != key_other_split


def test_form_flow_reuses_the_key_for_an_unchanged_triple():
    # The same (schema, pages, option_fields) triple reuses one published Flow.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    _, first = build_form_flow(schema, None, {"note"})
    _, second = build_form_flow(schema, None, {"note"})
    assert first == second


def test_form_flow_option_bearing_string_renders_a_dropdown_and_keys_its_own_flow():
    # A plain string property the ask marks option-bearing renders a dynamic Dropdown
    # (parity with web/Slack), and that set joins the publish key.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}

    plain_json, enum_only_key = build_form_flow(schema)
    option_json, option_key = build_form_flow(schema, None, {"note"})

    assert _controls(plain_json)[0]["type"] == "TextInput"
    dropdown = _controls(option_json)[0]
    assert dropdown["type"] == "Dropdown"
    assert dropdown["data-source"] == "${data.note__ds}"
    assert dropdown["init-value"] == "${data.note__init}"
    assert option_key != enum_only_key


def test_page_button_label_labels_the_footer_else_the_generic_default():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [
        {"title": "First", "fields": ["a"], "button_label": "Next step"},
        {"title": "Second", "fields": ["b"], "button_label": "Send it"},
    ]
    flow_json, _ = build_form_flow(schema, pages)
    assert _screen_children(flow_json, 0)[-1]["label"] == "Next step"  # the step footer
    assert _screen_children(flow_json, 1)[-1]["label"] == "Send it"  # the terminal footer
    # A page with no button_label keeps the generic defaults.
    plain, _ = build_form_flow(schema, [{"title": "1", "fields": ["a"]}, {"title": "2", "fields": ["b"]}])
    assert _screen_children(plain, 0)[-1]["label"] == "Continue"
    assert _screen_children(plain, 1)[-1]["label"] == "Submit"


def test_page_button_label_over_the_cap_is_refused_naming_the_page():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    pages = [{"title": "Only", "fields": ["a"], "button_label": "x" * 36}]
    with pytest.raises(ChannelInputError, match="'Only'"):
        build_form_flow(schema, pages)


def test_form_title_is_the_page_less_screen_title_else_form():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    titled, _ = build_form_flow(schema, form_title="Your details")
    assert titled["screens"][0]["title"] == "Your details"
    default, _ = build_form_flow(schema)
    assert default["screens"][0]["title"] == "Form"


def test_form_title_rekeys_a_page_less_flow_but_not_a_paged_one():
    # A page-less form_title is Flow JSON (the screen title), so it re-keys the published Flow; a
    # paged form shows each step's own title, so form_title does not change its emitted shape or key.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    _, plain = build_form_flow(schema)
    _, titled = build_form_flow(schema, form_title="Your details")
    assert plain != titled
    pages = [{"title": "Step", "fields": ["note"]}]
    _, paged = build_form_flow(schema, pages)
    _, paged_titled = build_form_flow(schema, pages, form_title="Ignored")
    assert paged == paged_titled


def test_form_flow_key_folds_the_emitted_shape():
    # The key hashes the emitted flow_json, so a change to the emitted shape alone
    # re-keys — a corrected shape can never resolve a Flow published under an old shape.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    flow_json, key = build_form_flow(schema)
    resolved_pages = [{"title": "Form", "fields": ["note"]}]
    assert _canonical_hash_pages(schema, resolved_pages, set(), flow_json) == key
    mutated = {**flow_json, "screens": []}
    assert _canonical_hash_pages(schema, resolved_pages, set(), mutated) != key


def test_form_flow_unknown_page_field_raises():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pytest.raises(ChannelInputError, match="ghost"):
        build_form_flow(schema, [{"title": "P", "fields": ["ghost"]}])


# -- build_flow_data (the per-send prefill/option data) ------------------------


def test_flow_data_carries_values_and_option_data_sources():
    schema = {
        "type": "object",
        "properties": {"tier": {"type": "string", "enum": ["gold"]}, "note": {"type": "string"}},
    }
    values = {"note": "hello"}
    options = {"tier": [{"value": "g", "label": "Gold"}, {"value": "s", "label": "Silver"}]}

    data = build_flow_data(schema, values, options)

    assert data["note__init"] == "hello"
    assert data["tier__init"] == ""
    # The per-send option list overrides the schema enum for this send.
    assert data["tier__ds"] == [{"id": "g", "title": "Gold"}, {"id": "s", "title": "Silver"}]


def test_flow_data_defaults_a_dropdown_to_the_schema_enum():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["gold", "silver"]}}}
    data = build_flow_data(schema, {}, {})
    assert data["tier__ds"] == [{"id": "gold", "title": "gold"}, {"id": "silver", "title": "silver"}]


def test_flow_data_per_send_options_on_a_plain_string_field_build_its_data_source():
    # Parity with web/Slack: an option-bearing plain string (no schema enum) takes a
    # per-send list — its published Flow renders a dynamic dropdown for it.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}

    data = build_flow_data(schema, {}, {"note": [{"value": "x", "label": "X"}, {"value": "y"}]})

    assert data["note__init"] == ""
    assert data["note__ds"] == [{"id": "x", "title": "X"}, {"id": "y", "title": "y"}]


def test_flow_data_per_send_options_on_a_non_string_field_raise_naming_it():
    # Only a string maps to a dropdown; options on a boolean can never be honored.
    schema = {"type": "object", "properties": {"agree": {"type": "boolean"}}}
    with pytest.raises(ChannelInputError, match="agree"):
        build_flow_data(schema, {}, {"agree": [{"value": "x"}]})


# -- the second line: a field's helper-text / caption and an option's description ----------


def test_field_description_renders_as_helper_text_on_text_and_date_controls():
    # A field's schema ``description`` draws as the control's ``helper-text`` on the controls Meta
    # allows it on (TextInput — string and number — and the date CalendarPicker).
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "Keep it short"},
            "count": {"type": "integer", "description": "A whole number"},
            "day": {"type": "string", "format": "date", "description": "When to start"},
            "slot": {"type": "string", "format": "date", "minDate": "2026-01-01", "description": "Within the window"},
        },
    }
    by_name = {c["name"]: c for c in _controls(build_form_flow(schema)[0])}
    assert [by_name[n]["type"] for n in ("note", "count", "day", "slot")] == [
        "TextInput",
        "TextInput",
        "CalendarPicker",
        "CalendarPicker",
    ]
    assert by_name["note"]["helper-text"] == "Keep it short"
    assert by_name["count"]["helper-text"] == "A whole number"
    assert by_name["day"]["helper-text"] == "When to start"
    assert by_name["slot"]["helper-text"] == "Within the window"


def test_choice_and_boolean_field_description_renders_as_a_text_caption_after_the_control():
    # A Dropdown/RadioButtonsGroup/CheckboxGroup/OptIn cannot hold ``helper-text`` (Meta), so a
    # field's second line on one draws as a sibling ``TextCaption`` immediately after the control —
    # the content is never dropped.
    schema = {
        "type": "object",
        "properties": {
            "tier": {"type": "string", "enum": ["g", "s"], "description": "Pick one tier"},
            "agree": {"type": "boolean", "description": "Read the terms first"},
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    types = [c["type"] for c in children if c["type"] != "Footer"]
    # Each control is immediately followed by its caption.
    assert types == ["RadioButtonsGroup", "TextCaption", "OptIn", "TextCaption"]
    captions = [c["text"] for c in children if c["type"] == "TextCaption"]
    assert captions == ["Pick one tier", "Read the terms first"]
    # The control carries no helper-text (Meta would reject it on these controls).
    assert "helper-text" not in next(c for c in children if c["type"] == "RadioButtonsGroup")


def test_textarea_field_description_renders_as_helper_text():
    # A ``control: textarea`` draws a TextArea, which takes ``helper-text`` like a TextInput.
    schema = {
        "type": "object",
        "properties": {"bio": {"type": "string", "control": "textarea", "description": "A few lines"}},
    }
    children = _screen_children(build_form_flow(schema)[0])
    area = next(c for c in children if c["type"] == "TextArea")
    assert area["helper-text"] == "A few lines"
    assert all(c["type"] != "TextCaption" for c in children)


def test_chips_field_description_renders_as_a_text_caption_after_the_control():
    # A ChipsSelector takes no ``helper-text``, so its field's second line is a sibling caption.
    schema = {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "enum": ["a", "b", "c"], "control": "chips", "description": "Tap one"}
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    types = [c["type"] for c in children if c["type"] != "Footer"]
    assert types == ["ChipsSelector", "TextCaption"]
    assert children[1]["text"] == "Tap one"
    assert "helper-text" not in children[0]


def test_merged_range_pair_descriptions_render_as_captions_after_the_range_control():
    # A merged ``rangeStart`` pair draws ONE range CalendarPicker, so each end's second line is its
    # own caption after the control, START first; an end without one draws none.
    schema = _range_schema(description="The last day")
    schema["properties"]["start"]["description"] = "The first day"
    children = _screen_children(build_form_flow(schema)[0])
    types = [c["type"] for c in children if c["type"] != "Footer"]
    assert types == ["CalendarPicker", "TextCaption", "TextCaption"]
    assert [c["text"] for c in children if c["type"] == "TextCaption"] == ["The first day", "The last day"]
    assert "helper-text" not in children[0]

    only_end = _range_schema(description="The last day")
    children = _screen_children(build_form_flow(only_end)[0])
    assert [c["text"] for c in children if c["type"] == "TextCaption"] == ["The last day"]


def test_conditional_choice_field_caption_rides_inside_the_if():
    # A choice field shown by a ``visibleWhen`` carries its caption INSIDE the same If, so the
    # caption shows and hides with the control.
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "tier": {
                "type": "string",
                "enum": ["g", "s"],
                "visibleWhen": {"field": "role", "equals": "admin"},
                "description": "Pick one tier",
            },
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    conditional = next(c for c in children if c.get("type") == "If")
    inner = [node["type"] for node in conditional["then"]]
    assert inner == ["RadioButtonsGroup", "TextCaption"]
    assert conditional["then"][1]["text"] == "Pick one tier"


def test_blank_field_description_draws_neither_helper_text_nor_caption():
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "   "},
            "tier": {"type": "string", "enum": ["g"], "description": ""},
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    assert all(c["type"] != "TextCaption" for c in children)
    assert "helper-text" not in next(c for c in children if c["type"] == "TextInput")


def test_option_description_rides_the_data_source_and_the_published_item_shape_is_stable():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["g"]}}}
    options = {
        "tier": [
            {"value": "g", "label": "Gold", "description": "2 hours, 50"},
            {"value": "s", "label": "Silver"},
        ]
    }
    data = build_flow_data(schema, {}, options)
    # The option carrying a second line gets a ``description`` item key; the one without omits it.
    assert data["tier__ds"] == [
        {"id": "g", "title": "Gold", "description": "2 hours, 50"},
        {"id": "s", "title": "Silver"},
    ]
    # The published Flow ALWAYS declares ``description`` on the data-source item type, so its shape
    # — and so its cache key — does not depend on a given send carrying option descriptions.
    screen = build_form_flow(schema, option_fields={"tier"})[0]["screens"][0]
    item_props = screen["data"]["tier__ds"]["items"]["properties"]
    assert set(item_props) == {"id", "title", "description"}


_CHIP_OPTIONS = {"pick": [{"value": "a", "label": "Gold", "description": "2-3 days"}, {"value": "b"}]}


def _chips_schema(enum: list[str]) -> dict:
    return {"type": "object", "properties": {"pick": {"type": "string", "enum": enum, "control": "chips"}}}


def test_chips_option_second_line_folds_onto_the_chip_title_and_declares_no_description():
    # A ChipsSelector item has no second-line slot, so the option's second line is folded onto the
    # chip title after an em dash, and the chips data-source declares only Meta's ``id``/``title``.
    schema = _chips_schema(["a", "b", "c"])
    assert build_flow_data(schema, {}, _CHIP_OPTIONS)["pick__ds"] == [
        {"id": "a", "title": "Gold \u2014 2-3 days"},
        {"id": "b", "title": "b"},
    ]
    screen = build_form_flow(schema, option_fields={"pick"})[0]["screens"][0]
    decl = screen["data"]["pick__ds"]
    assert set(decl["items"]["properties"]) == {"id", "title"}
    assert decl["__example__"] == [{"id": "a", "title": "a"}]


@pytest.mark.parametrize("count", [1, 21])
def test_degraded_chips_option_second_line_keeps_the_native_description(count: int):
    # A ``control: chips`` field outside Meta's 2..20 degrades to a radio/dropdown, which draws the
    # option's second line natively — the row keeps ``description`` and the item type declares it.
    schema = _chips_schema(["a", "b"] + [f"opt{n}" for n in range(count - 2)] if count > 2 else ["a"])
    assert build_flow_data(schema, {}, _CHIP_OPTIONS)["pick__ds"][0] == {
        "id": "a",
        "title": "Gold",
        "description": "2-3 days",
    }
    screen = build_form_flow(schema, option_fields={"pick"})[0]["screens"][0]
    assert set(screen["data"]["pick__ds"]["items"]["properties"]) == {"id", "title", "description"}


def test_option_description_over_meta_cap_is_refused_naming_the_property():
    # Meta caps a dropdown/radio/checkbox option description at 300 characters: a longer one is
    # refused at build naming the property and the option; a chip folds it with no documented cap.
    long_line = "d" * 301
    dropdown = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["g"]}}}
    with pytest.raises(ChannelInputError, match="option 'g' description is 301 characters"):
        build_flow_data(dropdown, {}, {"tier": [{"value": "g", "label": "Gold", "description": long_line}]})
    folded = build_flow_data(
        _chips_schema(["a", "b", "c"]),
        {},
        {"pick": [{"value": "a", "label": "Gold", "description": long_line}, {"value": "b"}]},
    )
    assert folded["pick__ds"] == [{"id": "a", "title": f"Gold \u2014 {long_line}"}, {"id": "b", "title": "b"}]


@pytest.mark.parametrize("count", [1, 21])
def test_chips_per_send_option_list_outside_meta_bounds_is_refused_naming_the_property(count: int):
    # Meta's ChipsSelector draws 2..20 choices. A field drawn as one (its schema enum is within the
    # bound) whose send carries a per-send list of another length is refused, naming the property,
    # the count and the bound — never a ChipsSelector fed rows past Meta's limit.
    schema = _chips_schema(["a", "b", "c"])
    options = {"pick": [{"value": f"v{n}"} for n in range(count)]}
    with pytest.raises(ChannelInputError, match=rf"'pick'.*{count} per-send options.*2\.\.20"):
        build_flow_data(schema, {}, options)
    # The bounds themselves pass.
    for ok in (2, 20):
        rows = build_flow_data(schema, {}, {"pick": [{"value": f"v{n}"} for n in range(ok)]})["pick__ds"]
        assert len(rows) == ok


def _second_line_schema(prop: dict, description: str) -> dict:
    return {"type": "object", "properties": {"f": {**prop, "description": description}}}


_HELPER_TEXT_PROPS = [
    {"type": "string"},
    {"type": "string", "control": "textarea"},
    {"type": "string", "format": "date"},
    {"type": "string", "format": "date", "minDate": "2026-01-01"},
]
_CAPTION_PROPS = [
    {"type": "string", "enum": ["g", "s"]},
    {"type": "boolean"},
    {"type": "string", "enum": ["a", "b", "c"], "control": "chips"},
]


def test_field_second_line_over_meta_caps_is_refused_naming_the_property():
    # Meta caps a field's helper text at 80 characters and a caption at 409: a longer second line
    # is refused at build naming the property; exactly the cap passes.
    for prop in _HELPER_TEXT_PROPS:
        with pytest.raises(ChannelInputError, match=r"'f' description is 81 characters.*helper text at 80"):
            build_form_flow(_second_line_schema(prop, "h" * 81))
        build_form_flow(_second_line_schema(prop, "h" * 80))
    for prop in _CAPTION_PROPS:
        with pytest.raises(ChannelInputError, match=r"'f' description is 410 characters.*caption at 409"):
            build_form_flow(_second_line_schema(prop, "c" * 410))
        build_form_flow(_second_line_schema(prop, "c" * 409))
    for end in ("start", "end"):
        schema = _range_schema()
        schema["properties"][end]["description"] = "c" * 410
        with pytest.raises(ChannelInputError, match=rf"'{end}' description is 410 characters.*caption at 409"):
            build_form_flow(schema)
        schema["properties"][end]["description"] = "c" * 409
        build_form_flow(schema)
    # A merged range end draws its second line as a caption, so the 80-character helper-text cap of a
    # lone date field does not apply to it.
    schema = _range_schema(description="c" * 100)
    assert [c["type"] for c in _screen_children(build_form_flow(schema)[0])][:2] == ["CalendarPicker", "TextCaption"]


# -- component_names: the identifier-safe naming rule --------------------------


def test_component_names_keeps_an_already_safe_key_verbatim():
    assert component_names({"note": {}, "qty_2": {}, "_x": {}}) == {"note": "note", "qty_2": "qty_2", "_x": "_x"}


def test_component_names_replaces_every_forbidden_character_with_underscore():
    # Every character outside [A-Za-z0-9_] becomes '_'; letters/digits are kept.
    assert component_names({"wamid.HBg=/status/4:language": {}}) == {
        "wamid.HBg=/status/4:language": "wamid_HBg__status_4_language"
    }


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("4score", "f_4score", id="leading-digit"),
        pytest.param("2.0", "f_2_0", id="leading-digit-after-sanitise"),
        pytest.param("=", "_", id="single-forbidden-becomes-underscore"),
        pytest.param("", "f_", id="empty"),
    ],
)
def test_component_names_prefixes_a_leading_digit_or_empty_result(key: str, expected: str):
    assert component_names({key: {}}) == {key: expected}


def test_component_names_disambiguates_collisions_in_schema_order():
    # Three distinct keys reduce to the same base 'a_b'; the first keeps it, the rest
    # take deterministic _2, _3 suffixes in schema order.
    mapping = component_names({"a.b": {}, "a_b": {}, "a/b": {}})
    assert mapping == {"a.b": "a_b", "a_b": "a_b_2", "a/b": "a_b_3"}


def test_component_names_never_emits_the_reserved_flow_token():
    # A key sanitising to 'flow_token' is disambiguated away from Meta's reserved key.
    assert component_names({"flow.token": {}}) == {"flow.token": "flow_token_2"}


# -- payload_labels: the human-readable completion-payload key rule ------------


def test_payload_labels_uses_the_title_else_the_property_key():
    labels = payload_labels({"start": {"type": "string", "title": "Start date"}, "note": {"type": "string"}})
    # Title when a string; the property key when there is none.
    assert labels == {"start": "Start date", "note": "note"}


def test_payload_labels_allows_spaces_punctuation_and_unicode():
    labels = payload_labels(
        {
            "quantity": {"type": "integer", "title": "Quantity"},
            "email": {"type": "string", "title": "E-mail"},
            "uni": {"type": "string", "title": "Ünïcödé"},
        }
    )
    assert labels == {"quantity": "Quantity", "email": "E-mail", "uni": "Ünïcödé"}


def test_payload_labels_disambiguates_colliding_labels_in_schema_order():
    # Two properties share a title; the first keeps it, the rest take deterministic _2, _3
    # suffixes in schema order — the same collision convention component_names uses.
    labels = payload_labels(
        {
            "a": {"type": "string", "title": "Name"},
            "b": {"type": "string", "title": "Name"},
            "c": {"type": "string", "title": "Name"},
        }
    )
    assert labels == {"a": "Name", "b": "Name_2", "c": "Name_3"}
    # Injective, so the reverse (label -> key) is lossless.
    assert len(set(labels.values())) == len(labels)


def test_payload_labels_disambiguates_a_title_equal_to_the_reserved_flow_token():
    # A property titled "flow_token" would otherwise key the completion payload with the exact
    # correlation token Meta injects into every reply (and the inbound decode strips); the reserved
    # guard bumps it to flow_token_2 — mirroring component_names — so no completion key clashes with it.
    labels = payload_labels({"when": {"type": "string", "title": "flow_token"}})
    assert labels == {"when": "flow_token_2"}


def test_terminal_completion_payload_is_keyed_by_labels_with_component_reference_values():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "title": "Start date"},
            "a.b=/c/4:d": {"type": "integer", "title": "Quantity"},
        },
        "required": [],
    }
    flow_json, _ = build_form_flow(schema)
    footer = _screen_children(flow_json, 0)[-1]
    action = footer["on-click-action"]
    assert action["name"] == "complete"
    # Keys are the human-readable labels; values are the identifier-safe component references
    # (the odd property key never leaks into a reference).
    names = component_names(schema["properties"])
    assert action["payload"] == {
        "Start date": f"${{form.{names['start']}}}",
        "Quantity": f"${{form.{names['a.b=/c/4:d']}}}",
    }
    # Every wire name/data key/reference and the navigate-payload keys stay identifier-safe.
    _assert_only_identifier_safe_wire_names(flow_json)


# -- odd property names ride only identifier-safe wire names -------------------

_ODD_SCHEMA = {
    "type": "object",
    "properties": {
        "wamid.HBg=/status/4:language": {"type": "string", "title": "Language"},
        "a.b=/c/4:d": {"type": "integer", "title": "Count"},
        "a.b": {"type": "string", "title": "First"},
        "a_b": {"type": "boolean", "title": "Second"},
    },
    "required": ["wamid.HBg=/status/4:language"],
}


def test_odd_named_schema_round_trips_to_identifier_safe_component_names():
    mapping = component_names(_ODD_SCHEMA["properties"])
    assert mapping == {
        "wamid.HBg=/status/4:language": "wamid_HBg__status_4_language",
        "a.b=/c/4:d": "a_b__c_4_d",
        "a.b": "a_b",
        "a_b": "a_b_2",
    }
    # The component name a colliding pair shares is disambiguated, never duplicated.
    assert len(set(mapping.values())) == len(mapping)


def test_odd_named_flow_carries_only_identifier_safe_names_everywhere():
    # Every name, data key, reference and payload key in the emitted Flow is identifier-safe,
    # and no raw odd property key leaks into a wire name (labels aside, checked below).
    flow_json, _ = build_form_flow(_ODD_SCHEMA)
    _assert_only_identifier_safe_wire_names(flow_json)

    blob = json.dumps(flow_json)
    for raw_key in _ODD_SCHEMA["properties"]:
        if _IDENTIFIER_RE.match(raw_key):
            continue  # an already-safe key legitimately appears verbatim as its component name
        # A key carrying forbidden characters appears ONLY as a label value (its title
        # here), never as a component name, data key or reference.
        assert f'"name": "{raw_key}"' not in blob
        assert f"${{data.{raw_key}" not in blob
        assert f"${{form.{raw_key}" not in blob

    # The labels still carry the human-facing titles verbatim.
    controls = _controls(flow_json)
    assert [c["label"] for c in controls] == ["Language", "Count", "First", "Second"]


def test_odd_named_multi_page_flow_carries_only_identifier_safe_names():
    pages = [
        {"title": "One", "fields": ["wamid.HBg=/status/4:language", "a.b=/c/4:d"]},
        {"title": "Two", "fields": ["a.b", "a_b"]},
    ]
    flow_json, _ = build_form_flow(_ODD_SCHEMA, pages)
    _assert_only_identifier_safe_wire_names(flow_json)


def test_odd_named_flow_data_keys_match_the_flow_component_names():
    # build_flow_data emits the SAME identifier-safe data keys the published Flow declares.
    flow_json, _ = build_form_flow(_ODD_SCHEMA)
    data = build_flow_data(_ODD_SCHEMA, {"a.b=/c/4:d": 3, "a.b": "hi", "a_b": True}, {})

    declared_keys: set[str] = set()
    for screen in flow_json["screens"]:
        declared_keys.update(screen.get("data", {}).keys())

    assert set(data) <= declared_keys
    mapping = component_names(_ODD_SCHEMA["properties"])
    # The prefilled value rides under the odd field's component-named __init key.
    assert data[f"{mapping['a.b=/c/4:d']}__init"] == "3"
    assert data[f"{mapping['a.b']}__init"] == "hi"
    assert data[f"{mapping['a_b']}__init"] is True


# -- E: multiple choice (CheckboxGroup) + radio/dropdown threshold ----------------


def _field_by_name(flow_json: dict, name: str, index: int = 0) -> dict:
    return next(child for child in _controls(flow_json, index) if child.get("name") == name)


def test_array_of_strings_maps_to_checkbox_group_with_selection_bounds():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ["a", "b", "c"]},
                "minItems": 1,
                "maxItems": 2,
            }
        },
        "required": ["tags"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "CheckboxGroup",
        "name": "tags",
        "label": "tags",
        "required": True,
        "data-source": "${data.tags__ds}",
        "init-value": "${data.tags__init}",
        "min-selected-items": 1,
        "max-selected-items": 2,
        "error-message": "${data.tags__err}",
    }


def test_required_array_without_min_items_demands_at_least_one():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}},
        "required": ["tags"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["min-selected-items"] == 1
    assert "max-selected-items" not in field


def test_array_of_strings_via_per_send_options_is_a_checkbox_group():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    field = _controls(build_form_flow(schema, option_fields={"tags"})[0])[0]
    assert field["type"] == "CheckboxGroup"


def test_array_of_strings_without_any_options_is_refused():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    with pytest.raises(ChannelInputError, match="CheckboxGroup"):
        build_form_flow(schema)


def test_array_of_non_strings_is_refused():
    schema = {"type": "object", "properties": {"nums": {"type": "array", "items": {"type": "integer"}}}}
    with pytest.raises(ChannelInputError, match="unsupported schema type"):
        build_form_flow(schema)


def test_checkbox_group_data_source_and_init_are_array_shaped():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}},
    }
    data = build_flow_data(schema, {"tags": ["a"]}, {})
    assert data["tags__init"] == ["a"]
    assert data["tags__ds"] == [{"id": "a", "title": "a"}, {"id": "b", "title": "b"}]


def test_array_prefill_defaults_to_empty_list():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a"]}}},
    }
    assert build_flow_data(schema, {}, {})["tags__init"] == []


def test_per_send_options_on_array_field_populate_the_data_source():
    from tai42_contract.interactions.models import FormOption  # noqa: F401  (shape documented inline)

    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    data = build_flow_data(schema, {}, {"tags": [{"value": "x", "label": "X"}]})
    assert data["tags__ds"] == [{"id": "x", "title": "X"}]


# -- C: date constraints (CalendarPicker) -----------------------------------------


def test_constrained_date_renders_a_bounded_calendar_picker():
    schema = {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "format": "date",
                "minDate": "2026-01-01",
                "maxDate": "2026-12-31",
                "unavailableDates": ["2026-07-04", "saturday", "sunday"],
            }
        },
        "required": ["when"],
    }
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "CalendarPicker"
    assert field["mode"] == "single"
    assert field["required"] is True
    assert field["min-date"] == "2026-01-01"
    assert field["max-date"] == "2026-12-31"
    assert field["unavailable-dates"] == ["2026-07-04"]
    # Excluded weekdays are dropped from include-days (the days that stay selectable).
    assert field["include-days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert field["init-value"] == "${data.when__init}"


def _range_schema(**end_extra: object) -> dict:
    return {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date", "title": "From"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "To", **end_extra},
        },
        "required": ["start", "end"],
    }


def test_same_page_range_pair_merges_into_one_range_calendar_picker():
    # A ``rangeStart`` pair on one page, same (absent) visibleWhen, no sibling dependant: ONE
    # range-mode CalendarPicker at the START's position; the END emits no control of its own.
    schema = _range_schema(minDays=3, maxDays=7)
    flow_json, _ = build_form_flow(schema)

    controls = _controls(flow_json)
    assert len(controls) == 1
    picker = controls[0]
    assert picker["type"] == "CalendarPicker"
    assert picker["name"] == "start"
    assert picker["mode"] == "range"
    # The two ends carry their own titles and required flags as the paired objects Meta's range takes.
    assert picker["label"] == {"start-date": "From", "end-date": "To"}
    assert picker["required"] == {"start-date": True, "end-date": True}
    assert picker["init-value"] == "${data.start__init}"
    # Meta counts the DIFFERENCE between the two dates; the platform span is the inclusive count.
    assert picker["min-days"] == 2
    assert picker["max-days"] == 6
    # One completion entry, under the START's label, carrying the range object; the END contributes none.
    footer = _screen_children(flow_json)[-1]
    assert set(footer["on-click-action"]["payload"]) == {"From"}
    assert footer["on-click-action"]["payload"]["From"] == "${form.start}"


def test_merged_range_bounds_are_never_looser_than_either_field():
    schema = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "format": "date",
                "minDate": "2026-01-01",
                "maxDate": "2026-06-30",
                "unavailableDates": ["2026-02-02", "saturday"],
            },
            "end": {
                "type": "string",
                "format": "date",
                "rangeStart": "start",
                "minDate": "2026-02-01",
                "maxDate": "2026-12-31",
                "unavailableDates": ["2026-03-03", "sunday"],
            },
        },
        "required": ["start", "end"],
    }
    picker = _controls(build_form_flow(schema)[0])[0]
    assert picker["min-date"] == "2026-02-01"  # the LATER minDate
    assert picker["max-date"] == "2026-06-30"  # the EARLIER maxDate
    assert set(picker["unavailable-dates"]) == {"2026-02-02", "2026-03-03"}  # the UNION
    # Both Saturday and Sunday are excluded (union), so include-days keeps Mon..Fri.
    assert picker["include-days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]


def test_range_pair_with_a_sibling_visible_when_on_an_end_draws_two_date_controls():
    # Folded tightening: a sibling whose visibleWhen names either end would read the merged control's
    # OBJECT value as a scalar, so the pair is NOT merged — it draws as two date controls and the
    # sibling's conditional reference resolves against the scalar START control.
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
            "note": {"type": "string", "visibleWhen": {"field": "start", "notEmpty": True}},
        },
        "required": ["start", "end"],
    }
    flow_json, _ = build_form_flow(schema)
    start = _field_by_name(flow_json, "start")
    end = _field_by_name(flow_json, "end")
    # Two separate single-mode date controls, not one range picker.
    assert (start["type"], start["mode"]) == ("CalendarPicker", "single")
    assert end["type"] == "CalendarPicker"
    assert end.get("mode") != "range"
    # The conditional note gates on the scalar START control's form value, not an object.
    note_if = next(child for child in _screen_children(flow_json) if child.get("type") == "If")
    assert note_if["condition"] == "${form.start} != ''"


def test_range_pair_with_a_multi_value_visible_when_draws_two_date_controls():
    # A shared multi-value ``in`` predicate splits a control into per-value cases, which the one
    # range control's single object value cannot express — so the pair draws as two date controls.
    predicate = {"field": "mode", "in": ["x", "y"]}
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["x", "y"]},
            "start": {"type": "string", "format": "date", "visibleWhen": predicate},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "visibleWhen": predicate},
        },
    }
    pages = [{"title": "P", "fields": ["mode", "start", "end"]}]
    flow_json, _ = build_form_flow(schema, pages)

    # The split fields are wrapped in per-value ``If`` nodes; collect every control type and mode.
    types: list[str] = []
    modes: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            ctype = node.get("type")
            if isinstance(ctype, str) and ctype != "If" and isinstance(node.get("name"), str):
                types.append(ctype)
                if isinstance(node.get("mode"), str):
                    modes.append(node["mode"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(flow_json["screens"][0]["layout"]["children"])
    # Two separate single-mode date controls, never a merged range picker.
    assert types.count("CalendarPicker") >= 2
    assert "range" not in modes


def test_range_pair_on_different_pages_draws_two_date_controls():
    schema = _range_schema()
    pages = [{"title": "First", "fields": ["start"]}, {"title": "Second", "fields": ["end"]}]
    flow_json, _ = build_form_flow(schema, pages)
    assert _field_by_name(flow_json, "start", 0)["mode"] == "single"
    assert _field_by_name(flow_json, "end", 1)["mode"] == "single"


def test_range_pair_with_a_review_page_draws_two_date_controls():
    # A review page reads each field's scalar ``__val`` back into a text line; a merged range
    # START's ``__val`` would be the range OBJECT, which Meta's text interpolation cannot render.
    # So the pair does NOT merge when the form has a review page — each end draws as its own scalar
    # date control whose ``__val`` the readback renders correctly (the documented degrade).
    schema = _range_schema()
    pages = [
        {"title": "Dates", "fields": ["start", "end"], "kind": "input"},
        {"title": "Review", "fields": [], "kind": "review"},
    ]
    flow_json, _ = build_form_flow(schema, pages)
    start = _field_by_name(flow_json, "start", 0)
    end = _field_by_name(flow_json, "end", 0)
    # Two separate single-mode date controls, not one range picker.
    assert (start["type"], start["mode"]) == ("CalendarPicker", "single")
    assert end["type"] == "CalendarPicker"
    assert end.get("mode") != "range"
    # The review readback references each field's own scalar carrier — never a range object.
    review_children = _screen_children(flow_json, 1)
    texts = [child["text"] for child in review_children if child["type"] == "TextBody"]
    assert "From: ${data.start__val}" in texts
    assert "To: ${data.end__val}" in texts


def test_merged_range_build_flow_data_emits_the_object_init_and_no_end_init():
    schema = _range_schema()
    data = build_flow_data(schema, {"start": "2026-05-01", "end": "2026-05-04"}, {})
    assert data["start__init"] == {"start-date": "2026-05-01", "end-date": "2026-05-04"}
    assert "end__init" not in data


def test_merged_range_build_flow_data_empty_object_when_unprefilled():
    schema = _range_schema()
    data = build_flow_data(schema, {}, {})
    assert data["start__init"] == {"start-date": "", "end-date": ""}
    assert "end__init" not in data


# -- D: display blocks + review screen --------------------------------------------


def test_display_blocks_render_in_order_ahead_of_the_inputs():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    pages = [
        {
            "title": "Form",
            "fields": ["note"],
            "kind": "input",
            "display": [
                {"kind": "heading", "text": "Welcome"},
                {"kind": "body", "text": "Please answer"},
                {"kind": "image", "src": "BASE64DATA", "alt": "a logo"},
            ],
        }
    ]
    children = _screen_children(build_form_flow(schema, pages)[0])
    assert children[0] == {"type": "TextHeading", "text": "Welcome"}
    assert children[1] == {"type": "TextBody", "text": "Please answer"}
    assert children[2] == {"type": "Image", "src": "BASE64DATA", "alt-text": "a logo"}
    # Then the input control, then the footer.
    assert children[3]["type"] == "TextInput"
    assert children[-1]["type"] == "Footer"


def test_display_slot_reads_dynamic_data_and_declares_its_key():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    pages = [{"title": "Form", "fields": ["note"], "kind": "input", "display": [{"kind": "body", "slot": "total"}]}]
    flow_json, _ = build_form_flow(schema, pages)
    body = _screen_children(flow_json)[0]
    assert body == {"type": "TextBody", "text": "${data.slot_total}"}
    assert flow_json["screens"][0]["data"]["slot_total"] == {"type": "string", "__example__": ""}


def test_review_page_renders_a_readback_of_every_field():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [
        {"title": "Fill", "fields": ["a", "b"], "kind": "input", "display": []},
        {"title": "Review", "fields": [], "kind": "review", "display": [{"kind": "heading", "text": "Please review"}]},
    ]
    flow_json, _ = build_form_flow(schema, pages)
    review_children = _screen_children(flow_json, 1)
    assert review_children[0] == {"type": "TextHeading", "text": "Please review"}
    # A readback TextBody per field, interpolating its collected value.
    texts = [child["text"] for child in review_children if child["type"] == "TextBody"]
    assert "a: ${data.a__val}" in texts
    assert "b: ${data.b__val}" in texts
    assert review_children[-1]["on-click-action"]["name"] == "complete"


# -- B: conditional (visibleWhen -> If) -------------------------------------------


def test_visible_when_equals_wraps_the_control_in_an_if():
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "equals": "admin"}},
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    conditional = next(child for child in children if child.get("type") == "If")
    assert conditional["condition"] == "${form.role} == 'admin'"
    assert conditional["then"][0]["name"] == "detail"


def test_visible_when_in_renders_sibling_ifs_per_value_never_an_or():
    # Meta refuses ``A == 'x' || A == 'y'`` ("Wrong positioning of operator '||'"). A field shown for
    # several values renders one ``If`` per value, each holding its own uniquely-named control.
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "in": ["a", "b"]}},
        },
    }
    flow_json = build_form_flow(schema)[0]
    assert "||" not in json.dumps(flow_json)
    ifs = [c for c in _screen_children(flow_json) if c.get("type") == "If"]
    assert [c["condition"] for c in ifs] == ["${form.role} == 'a'", "${form.role} == 'b'"]
    assert [c["then"][0]["name"] for c in ifs] == ["detail__a", "detail__b"]
    _assert_only_identifier_safe_wire_names(flow_json)


def test_visible_when_in_coalesces_the_split_answer_in_the_completion_payload():
    # The per-case controls carry one answer under the field's own label: a backtick concatenation of
    # both variant refs (mutually exclusive cases, so at most one is non-empty).
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "title": "Detail", "visibleWhen": {"field": "role", "in": ["a", "b"]}},
        },
    }
    payload = _screen_children(build_form_flow(schema)[0])[-1]["on-click-action"]["payload"]
    assert payload["Detail"] == "`${form.detail__a}${form.detail__b}`"
    assert payload["role"] == "${form.role}"  # an unsplit field keeps its plain single reference


def test_visible_when_single_value_in_stays_one_plain_if():
    # A single-entry ``in`` is one value: one ``If``, the control keeps its plain name (no suffix) —
    # byte-identical to an ``equals`` predicate, no needless split.
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "in": ["a"]}},
        },
    }
    ifs = [c for c in _screen_children(build_form_flow(schema)[0]) if c.get("type") == "If"]
    assert len(ifs) == 1
    assert ifs[0]["condition"] == "${form.role} == 'a'"
    assert ifs[0]["then"][0]["name"] == "detail"


def test_visible_when_chained_dependent_nests_under_its_controllers_case():
    # A field shown on a MULTI-VALUE field renders nested ``If`` per case (no ``||``), both uniquely
    # named; each answer coalesces across cases under its own completion label.
    schema = {
        "type": "object",
        "properties": {
            "pick": {"type": "string"},
            "extra": {
                "type": "string",
                "title": "Extra",
                "visibleWhen": {"field": "pick", "in": ["alpha", "beta"]},
            },
            "extra_note": {
                "type": "string",
                "title": "Extra note",
                "visibleWhen": {"field": "extra", "equals": "other"},
            },
        },
    }
    flow_json = build_form_flow(schema)[0]
    assert "||" not in json.dumps(flow_json)
    children = _screen_children(flow_json)
    extra_ifs = [c for c in children if c.get("type") == "If" and c["then"][0].get("name", "").startswith("extra__")]
    assert [c["condition"] for c in extra_ifs] == ["${form.pick} == 'alpha'", "${form.pick} == 'beta'"]
    nested = [c for c in children if c.get("type") == "If" and c["then"][0].get("type") == "If"]
    assert len(nested) == 2
    assert nested[0]["condition"] == "${form.pick} == 'alpha'"
    inner = nested[0]["then"][0]
    assert inner["condition"] == "${form.extra__alpha} == 'other'"
    assert inner["then"][0]["name"] == "extra_note__alpha"
    payload = children[-1]["on-click-action"]["payload"]
    assert payload["Extra"] == "`${form.extra__alpha}${form.extra__beta}`"
    assert payload["Extra note"] == "`${form.extra_note__alpha}${form.extra_note__beta}`"
    _assert_only_identifier_safe_wire_names(flow_json)


def test_visible_when_in_over_an_earlier_screen_controller_reads_the_coalesced_carrier():
    # A multi-value controller on an EARLIER screen is read from its single coalesced ``__val`` carrier,
    # so the dependent is one If per value with no controller suffix.
    schema = {
        "type": "object",
        "properties": {
            "pick": {"type": "string"},
            "extra": {"type": "string", "visibleWhen": {"field": "pick", "in": ["alpha", "beta"]}},
        },
    }
    pages = [
        {"title": "First", "fields": ["pick"], "kind": "input", "display": []},
        {"title": "Second", "fields": ["extra"], "kind": "input", "display": []},
    ]
    ifs = [c for c in _screen_children(build_form_flow(schema, pages)[0], 1) if c.get("type") == "If"]
    assert [c["condition"] for c in ifs] == [
        "${data.pick__val} == 'alpha'",
        "${data.pick__val} == 'beta'",
    ]
    assert [c["then"][0]["name"] for c in ifs] == ["extra__alpha", "extra__beta"]


def test_visible_when_multi_value_on_a_boolean_or_array_field_is_refused():
    # A split field's answer coalesces as a backtick string concatenation (string-only). A boolean or
    # array field shown for several values cannot coalesce to one typed value — refused loudly, never
    # rendered into Meta-refused (boolean) or value-losing (array) Flow JSON.
    for field_type, extra in (("boolean", {}), ("array", {"items": {"type": "string", "enum": ["x", "y"]}})):
        schema = {
            "type": "object",
            "properties": {
                "pick": {"type": "string"},
                "opt": {"type": field_type, "visibleWhen": {"field": "pick", "in": ["alpha", "beta"]}, **extra},
            },
        }
        with pytest.raises(ChannelInputError, match="cannot coalesce to one typed value"):
            build_form_flow(schema, option_fields={"opt"})


def test_visible_when_chain_deeper_than_meta_nesting_is_refused():
    # Meta nests at most three ``If``; a dependency chain deeper than that raises, never emits bad JSON.
    props: dict = {"a": {"type": "string"}}
    prev = "a"
    for i in range(1, 5):  # a<-b<-c<-d<-e : e's chain is four deep
        key = chr(ord("a") + i)
        props[key] = {"type": "string", "visibleWhen": {"field": prev, "in": ["x", "y"]}}
        prev = key
    with pytest.raises(ChannelInputError, match="nests at most"):
        build_form_flow({"type": "object", "properties": props})


def test_visible_when_in_values_colliding_to_one_case_name_is_refused():
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "in": ["a!", "a?"]}},
        },
    }
    with pytest.raises(ChannelInputError, match="collide"):
        build_form_flow(schema)


def test_visible_when_empty_in_is_refused():
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "in": []}},
        },
    }
    with pytest.raises(ChannelInputError, match="names no value"):
        build_form_flow(schema)


def test_visible_when_not_empty_condition():
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "notEmpty": True}},
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    conditional = next(child for child in children if child.get("type") == "If")
    assert conditional["condition"] == "${form.role} != ''"


# -- A: reacting (endpoint-driven) flow -------------------------------------------


def test_reacting_field_change_publishes_endpoint_driven_with_a_data_exchange_action():
    schema = {"type": "object", "properties": {"pick": {"type": "string", "enum": ["a", "b"]}}, "required": ["pick"]}
    reactions = {"field_changed": ["pick"], "page_advanced": [], "submitted": False, "choices": []}
    flow_json, _ = build_form_flow(schema, reactions=reactions)
    assert flow_json["data_api_version"] == "3.0"
    assert flow_json["routing_model"] == {"SCREEN_A": []}
    field = _controls(flow_json)[0]
    action = field["on-select-action"]
    assert action["name"] == "data_exchange"
    assert action["payload"]["tai42_event"] == "field_changed"
    assert action["payload"]["tai42_field"] == "pick"
    assert action["payload"]["pick"] == "${form.pick}"


def test_reacting_field_change_on_a_chips_control_attaches_an_on_select_action():
    # A ChipsSelector is a selectable control, so a field_changed reaction attaches to it.
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": ["a", "b", "c"], "control": "chips"}},
        "required": ["pick"],
    }
    reactions = {"field_changed": ["pick"], "page_advanced": [], "submitted": False, "choices": []}
    field = _controls(build_form_flow(schema, reactions=reactions)[0])[0]
    assert field["type"] == "ChipsSelector"
    assert field["on-select-action"]["payload"]["tai42_event"] == "field_changed"


def test_reacting_submit_turns_the_terminal_footer_into_a_data_exchange():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    reactions = {"field_changed": [], "page_advanced": [], "submitted": True, "choices": []}
    flow_json, _ = build_form_flow(schema, reactions=reactions)
    footer = _screen_children(flow_json)[-1]
    assert footer["on-click-action"]["name"] == "data_exchange"
    assert footer["on-click-action"]["payload"]["tai42_event"] == "submitted"
    assert footer["on-click-action"]["payload"]["note"] == "${form.note}"


def test_reacting_page_advance_turns_the_step_footer_into_a_data_exchange():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [
        {"title": "First", "fields": ["a"], "kind": "input", "display": []},
        {"title": "Second", "fields": ["b"], "kind": "input", "display": []},
    ]
    reactions = {"field_changed": [], "page_advanced": ["First"], "submitted": False, "choices": []}
    flow_json, _ = build_form_flow(schema, pages, reactions=reactions)
    step_footer = _screen_children(flow_json, 0)[-1]
    assert step_footer["on-click-action"]["name"] == "data_exchange"
    assert step_footer["on-click-action"]["payload"]["tai42_event"] == "page_advanced"
    assert step_footer["on-click-action"]["payload"]["tai42_page"] == "First"


def test_field_changed_on_a_text_field_is_refused():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    reactions = {"field_changed": ["note"], "page_advanced": [], "submitted": False, "choices": []}
    with pytest.raises(ChannelInputError, match="selectable control"):
        build_form_flow(schema, reactions=reactions)


def test_reacting_form_property_colliding_with_a_reserved_marker_is_refused():
    schema = {"type": "object", "properties": {"tai42_event": {"type": "string"}}, "required": ["tai42_event"]}
    reactions = {"field_changed": [], "page_advanced": [], "submitted": True, "choices": []}
    with pytest.raises(ChannelInputError, match="reserved data-endpoint markers"):
        build_form_flow(schema, reactions=reactions)


def test_empty_reactions_block_stays_a_static_flow():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    reactions = {"field_changed": [], "page_advanced": [], "submitted": False, "choices": []}
    flow_json, _ = build_form_flow(schema, reactions=reactions)
    assert "data_api_version" not in flow_json
    assert _screen_children(flow_json)[-1]["on-click-action"]["name"] == "complete"


def test_options_on_a_non_choice_property_are_refused_in_flow_data():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(ChannelInputError, match="choice control"):
        build_flow_data(schema, {}, {"n": [{"value": "x"}]})


# -- branch coverage: choice-type, slots, conditions, reaction payloads -----------

from tai42_channel_whatsapp.flows import slot_datanames  # noqa: E402
from tai42_channel_whatsapp.flows_components import _choice_component_type  # noqa: E402


def test_choice_component_type_classifies_an_array_as_checkbox():
    assert _choice_component_type({"type": "array", "items": {"type": "string"}}) == "CheckboxGroup"


def test_slot_datanames_disambiguate_a_sanitisation_collision():
    pages = [{"title": "T", "fields": [], "display": [{"kind": "body", "slot": "a!"}, {"kind": "body", "slot": "a?"}]}]
    mapping = slot_datanames(pages)
    assert len(set(mapping.values())) == 2


def test_visible_when_bool_and_number_literals():
    schema = {
        "type": "object",
        "properties": {
            "agree": {"type": "boolean"},
            "n": {"type": "integer"},
            "x": {"type": "string", "visibleWhen": {"field": "agree", "equals": True}},
            "y": {"type": "string", "visibleWhen": {"field": "n", "equals": 3}},
        },
    }
    children = _screen_children(build_form_flow(schema)[0])
    conditions = [child["condition"] for child in children if child.get("type") == "If"]
    assert "${form.agree} == true" in conditions
    assert "${form.n} == 3" in conditions


def test_visible_when_on_earlier_screen_uses_the_val_carrier():
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "equals": "admin"}},
        },
    }
    pages = [
        {"title": "One", "fields": ["role"], "kind": "input", "display": []},
        {"title": "Two", "fields": ["detail"], "kind": "input", "display": []},
    ]
    conditional = next(c for c in _screen_children(build_form_flow(schema, pages)[0], 1) if c.get("type") == "If")
    assert conditional["condition"] == "${data.role__val} == 'admin'"


def test_visible_when_referencing_a_later_field_is_left_unwrapped():
    # The controlling field is on a LATER screen, unreadable here, so no client-side If is
    # emitted (the answer facet still enforces visibility).
    schema = {
        "type": "object",
        "properties": {
            "detail": {"type": "string", "visibleWhen": {"field": "role", "equals": "admin"}},
            "role": {"type": "string"},
        },
    }
    pages = [
        {"title": "One", "fields": ["detail"], "kind": "input", "display": []},
        {"title": "Two", "fields": ["role"], "kind": "input", "display": []},
    ]
    children = _screen_children(build_form_flow(schema, pages)[0], 0)
    assert all(child.get("type") != "If" for child in children)
    assert any(child.get("name") == "detail" for child in children)


def test_reacting_optin_field_change_uses_on_click_action():
    schema = {"type": "object", "properties": {"agree": {"type": "boolean"}}, "required": ["agree"]}
    reactions = {"field_changed": ["agree"], "page_advanced": [], "submitted": False, "choices": []}
    field = _controls(build_form_flow(schema, reactions=reactions)[0])[0]
    assert field["on-click-action"]["name"] == "data_exchange"


def test_reacting_submit_on_multipage_includes_earlier_values_via_val_carrier():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [
        {"title": "One", "fields": ["a"], "kind": "input", "display": []},
        {"title": "Two", "fields": ["b"], "kind": "input", "display": []},
    ]
    reactions = {"field_changed": [], "page_advanced": [], "submitted": True, "choices": []}
    footer = _screen_children(build_form_flow(schema, pages, reactions=reactions)[0], 1)[-1]
    payload = footer["on-click-action"]["payload"]
    assert payload["a"] == "${data.a__val}"
    assert payload["b"] == "${form.b}"


def test_reacting_field_changed_naming_unknown_field_is_refused():
    schema = {"type": "object", "properties": {"a": {"type": "string", "enum": ["x"]}}}
    reactions = {"field_changed": ["missing"], "page_advanced": [], "submitted": False, "choices": []}
    with pytest.raises(ChannelInputError, match="unknown property"):
        build_form_flow(schema, reactions=reactions)


def test_build_flow_data_requires_a_properties_object():
    with pytest.raises(ChannelInputError, match="non-empty 'properties'"):
        build_flow_data({"type": "object"}, {}, {})


def test_build_flow_data_array_without_options_or_enum_is_refused():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    with pytest.raises(ChannelInputError, match="data-source"):
        build_flow_data(schema, {}, {})


# -- branch coverage: chips refusal, button-label cap, merged-range edges ---------

from tai42_channel_whatsapp.flows import _attach_field_reaction  # noqa: E402
from tai42_channel_whatsapp.flows_components import range_pairs  # noqa: E402


def test_plain_string_chips_control_without_an_enum_is_refused():
    # A ``control: chips`` on a plain string (no declared choice list) cannot draw a ChipsSelector, so
    # it is refused loudly rather than silently drawn as plain text.
    schema = {"type": "object", "properties": {"pick": {"type": "string", "control": "chips"}}}
    with pytest.raises(ChannelInputError, match="chips"):
        build_form_flow(schema)


def test_page_button_label_that_is_not_a_string_is_refused():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    pages = [{"title": "P", "fields": ["a"], "button_label": 123}]
    with pytest.raises(ChannelInputError, match="button_label must be a string"):
        build_form_flow(schema, pages)


def test_range_start_pair_on_non_date_fields_draws_two_plain_controls():
    # ``rangeStart`` merges only a pair of date fields; on plain strings the pair never merges and both
    # ends draw as their own TextInput.
    schema = {
        "type": "object",
        "properties": {"start": {"type": "string"}, "end": {"type": "string", "rangeStart": "start"}},
    }
    controls = _controls(build_form_flow(schema)[0])
    assert {c["name"] for c in controls} == {"start", "end"}
    assert all(c["type"] == "TextInput" for c in controls)


def test_range_pairs_skips_a_non_dict_property():
    # A property whose schema is not an object contributes no merge (guarded before any date check).
    assert range_pairs(["start", "end"], {"start": {"type": "string", "format": "date"}, "end": "oops"}) == {}


def test_merged_range_union_skips_a_non_string_unavailable_entry():
    # A non-string entry in an end's ``unavailableDates`` is ignored when the merged control's exclusions
    # are unioned; only the real date string survives.
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date", "unavailableDates": ["2026-01-01", 5]},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
        },
        "required": ["start", "end"],
    }
    picker = _controls(build_form_flow(schema)[0])[0]
    assert picker["type"] == "CalendarPicker"
    assert picker["unavailable-dates"] == ["2026-01-01"]


def test_merged_range_prefill_that_is_not_an_iso_date_is_refused():
    schema = _range_schema()
    with pytest.raises(ChannelInputError, match="YYYY-MM-DD"):
        build_flow_data(schema, {"start": "not-a-date"}, {})


def test_visible_when_in_value_with_no_identifier_safe_characters_is_refused():
    # A multi-value ``in`` splits into per-value case names; a value that slugs to the empty string
    # cannot name a case and is refused loudly.
    schema = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "detail": {"type": "string", "visibleWhen": {"field": "role", "in": ["ok", "!!!"]}},
        },
    }
    with pytest.raises(ChannelInputError, match="no identifier-safe characters"):
        build_form_flow(schema)


def test_attach_field_reaction_on_a_non_selectable_control_is_refused():
    # A plain text control has no on-change event on WhatsApp; attaching a field_changed reaction to one
    # is refused loudly.
    component = {"type": "TextInput", "name": "note", "label": "note"}
    with pytest.raises(ChannelInputError, match="selectable control"):
        _attach_field_reaction(component, "note", ["note"], [], {"note": "note"}, {"note": [("", [])]}, {}, {})


def test_merged_range_data_model_on_a_later_screen_omits_the_end_init():
    # The merged pair sits on the SECOND screen; its ``data`` declares the START's range ``init`` and no
    # ``init`` for the merged-away END.
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string"},
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
        },
    }
    pages = [
        {"title": "Note", "fields": ["note"], "kind": "input", "display": []},
        {"title": "Dates", "fields": ["start", "end"], "kind": "input", "display": []},
    ]
    data = build_form_flow(schema, pages)[0]["screens"][1]["data"]
    assert "start__init" in data
    assert "end__init" not in data


def test_merged_range_val_carrier_on_a_later_screen_omits_the_end_val():
    # The merged pair sits on the FIRST screen; a later screen declares the START's range ``__val`` and
    # no ``__val`` for the merged-away END.
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
            "note": {"type": "string"},
        },
    }
    pages = [
        {"title": "Dates", "fields": ["start", "end"], "kind": "input", "display": []},
        {"title": "Note", "fields": ["note"], "kind": "input", "display": []},
    ]
    data = build_form_flow(schema, pages)[0]["screens"][1]["data"]
    assert "start__val" in data
    assert "end__val" not in data


def test_merged_range_step_footer_forwards_only_the_start_carrier():
    # A 3-screen form with the merged pair on the FIRST screen: the navigate payloads carry the START's
    # range ``__val`` and nothing for the merged END, both from the this-screen and the earlier-screen
    # forward loops.
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
            "mid": {"type": "string"},
            "note": {"type": "string"},
        },
    }
    pages = [
        {"title": "Dates", "fields": ["start", "end"], "kind": "input", "display": []},
        {"title": "Mid", "fields": ["mid"], "kind": "input", "display": []},
        {"title": "Note", "fields": ["note"], "kind": "input", "display": []},
    ]
    flow_json = build_form_flow(schema, pages)[0]
    first = _screen_children(flow_json, 0)[-1]["on-click-action"]["payload"]
    assert "start__val" in first
    assert "end__val" not in first
    second = _screen_children(flow_json, 1)[-1]["on-click-action"]["payload"]
    assert "start__val" in second
    assert "end__val" not in second


def test_merged_range_step_footer_forwards_only_the_start_init_to_a_later_pair():
    # A 3-screen form with the merged pair on the SECOND screen: the first screen's navigate forwards the
    # START's range ``init`` downstream and nothing for the merged END (the later-screen forward loop).
    schema = {
        "type": "object",
        "properties": {
            "mid": {"type": "string"},
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
            "note": {"type": "string"},
        },
    }
    pages = [
        {"title": "Mid", "fields": ["mid"], "kind": "input", "display": []},
        {"title": "Dates", "fields": ["start", "end"], "kind": "input", "display": []},
        {"title": "Note", "fields": ["note"], "kind": "input", "display": []},
    ]
    first = _screen_children(build_form_flow(schema, pages)[0], 0)[-1]["on-click-action"]["payload"]
    assert "start__init" in first
    assert "end__init" not in first


def test_submitted_reaction_payload_omits_the_merged_range_end_same_screen():
    # A reacting ``submitted`` single-screen form: the terminal data_exchange payload carries the START's
    # range reference and no entry for the merged-away END (the this-screen payload loop).
    schema = _range_schema()
    reactions = {"field_changed": [], "page_advanced": [], "submitted": True, "choices": []}
    payload = _screen_children(build_form_flow(schema, reactions=reactions)[0])[-1]["on-click-action"]["payload"]
    assert payload["start"] == "${form.start}"
    assert "end" not in payload


def test_submitted_reaction_payload_omits_the_merged_range_end_earlier_screen():
    # A reacting ``submitted`` 2-screen form with the merged pair on the FIRST screen: the terminal
    # payload carries the START's ``__val`` carrier and no entry for the merged-away END.
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
            "note": {"type": "string"},
        },
    }
    pages = [
        {"title": "Dates", "fields": ["start", "end"], "kind": "input", "display": []},
        {"title": "Note", "fields": ["note"], "kind": "input", "display": []},
    ]
    reactions = {"field_changed": [], "page_advanced": [], "submitted": True, "choices": []}
    footer = _screen_children(build_form_flow(schema, pages, reactions=reactions)[0], 1)[-1]
    payload = footer["on-click-action"]["payload"]
    assert payload["start"] == "${data.start__val}"
    assert "end" not in payload


def test_field_changed_on_a_merged_range_end_attaches_the_reaction_to_the_range_control():
    # A ``field_changed`` naming either end of a merged pair attaches the one on-select-action to the
    # single range control, named by the end that declared the trigger.
    schema = _range_schema()
    reactions = {"field_changed": ["end"], "page_advanced": [], "submitted": False, "choices": []}
    picker = _controls(build_form_flow(schema, reactions=reactions)[0])[0]
    assert picker["type"] == "CalendarPicker"
    assert picker["mode"] == "range"
    assert picker["on-select-action"]["name"] == "data_exchange"
    assert picker["on-select-action"]["payload"]["tai42_field"] == "end"
