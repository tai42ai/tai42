"""``flows.build_form_flow`` — the answer-schema → publishable Flow JSON mapping: the
supported type subset, the unsupported-shape rejections, the letters-and-underscores
screen ids, the no-``Form`` control-level prefill, and the publish key."""

from __future__ import annotations

import re

import pytest
from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows import (
    FORM_ENTRY_SCREEN,
    _canonical_hash_pages,
    build_flow_data,
    build_form_flow,
)


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
    # value through the unwrapped-component reference, an earlier one from its __val carrier.
    footer = _screen_children(flow_json, 1)[-1]
    assert footer["on-click-action"]["name"] == "complete"
    assert footer["on-click-action"]["payload"] == {"a": "${data.a__val}", "b": "${screen.b}"}
    # The first step navigates forward, carrying its collected value on as ${screen.<field>}.
    step_footer = _screen_children(flow_json, 0)[-1]
    assert step_footer["on-click-action"]["name"] == "navigate"
    assert step_footer["on-click-action"]["next"] == {"type": "screen", "name": "SCREEN_B"}
    assert step_footer["on-click-action"]["payload"]["a__val"] == "${screen.a}"


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


def test_string_enum_maps_to_dynamic_dropdown():
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": ["a", "b", "c"]}},
        "required": ["pick"],
    }

    field = _controls(build_form_flow(schema)[0])[0]
    assert field == {
        "type": "Dropdown",
        "name": "pick",
        "label": "pick",
        "required": True,
        # Dynamic, so a per-send option list can replace the choices without republishing.
        "data-source": "${data.pick__ds}",
        "init-value": "${data.pick__init}",
    }


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


def test_local_field_references_use_the_unwrapped_component_spelling():
    # A value on THIS screen is read as ${screen.<field>} (no Form namespace); an
    # earlier screen's value rides forward as ${data.<field>__val}.
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [{"title": "1", "fields": ["a"]}, {"title": "2", "fields": ["b"]}]

    flow_json, _ = build_form_flow(schema, pages)

    step_payload = _screen_children(flow_json, 0)[-1]["on-click-action"]["payload"]
    assert step_payload["a__val"] == "${screen.a}"  # this-screen value, unwrapped-component spelling
    terminal_payload = _screen_children(flow_json, 1)[-1]["on-click-action"]["payload"]
    assert terminal_payload["b"] == "${screen.b}"  # this-screen value on the terminal
    assert terminal_payload["a"] == "${data.a__val}"  # earlier value from its carrier


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
