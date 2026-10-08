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
            if node.get("type") in {"TextInput", "Dropdown", "OptIn", "DatePicker"} and isinstance(
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
    assert field == {
        "type": "TextInput",
        "name": "qty",
        "label": "qty",
        "required": True,
        "input-type": "number",
        "init-value": "${data.qty__init}",
    }


# -- the format: date → DatePicker mapping ------------------------------------


def test_string_with_format_date_maps_to_date_picker_without_required():
    # A DatePicker carries no ``required`` field: the vendor's component reference defines
    # one for every other input control but not for the DatePicker, and Meta rejects an
    # unknown component property at publish.
    schema = {
        "type": "object",
        "properties": {"when": {"type": "string", "format": "date", "title": "Pick a date"}},
        "required": ["when"],
    }

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "DatePicker",
        "name": "when",
        "label": "Pick a date",
        "init-value": "${data.when__init}",
    }
    assert "required" not in field


@pytest.mark.parametrize("fmt", ["time", "date-time"])
def test_string_with_format_time_or_date_time_stays_text_input(fmt: str):
    # The vendor has no time-of-day picker, so these render as a plain text input; the ask
    # door validates the submitted shape.
    schema = {"type": "object", "properties": {"at": {"type": "string", "format": fmt}}, "required": ["at"]}

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "TextInput",
        "name": "at",
        "label": "at",
        "required": True,
        "init-value": "${data.at__init}",
    }


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
    # allows it on (TextInput — string and number — DatePicker, CalendarPicker).
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
        "DatePicker",
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


def test_unconstrained_date_stays_a_bare_date_picker():
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": "date"}}, "required": ["when"]}
    field = _controls(build_form_flow(schema)[0])[0]
    assert field["type"] == "DatePicker"
    assert "required" not in field


def test_declared_range_renders_two_scalar_date_controls_each_submitting_its_own_field():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 1, "maxDays": 7},
        },
        "required": ["start", "end"],
    }
    flow_json, _ = build_form_flow(schema)
    start = _field_by_name(flow_json, "start")
    end = _field_by_name(flow_json, "end")
    # The end field carries a range pairing, so it is a (constrained) CalendarPicker; the start
    # is a bare DatePicker. The span is enforced at the answer facet, not drawn — so the
    # completion submits the TWO scalar date fields, nothing recombined.
    assert start["type"] == "DatePicker"
    assert end["type"] == "CalendarPicker"
    footer = _screen_children(flow_json)[-1]
    assert set(footer["on-click-action"]["payload"]) == {"start", "end"}
    assert footer["on-click-action"]["payload"]["start"] == "${form.start}"
    assert footer["on-click-action"]["payload"]["end"] == "${form.end}"


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
    with pytest.raises(ChannelInputError, match="reserved reaction"):
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
