"""``flows.build_flow`` — the answer-schema → Flow JSON mapping, the supported
type subset, the unsupported-shape rejections, and the canonical schema hash."""

from __future__ import annotations

import hashlib
import json

import pytest
from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows import build_flow


def _children(flow_json: dict) -> list[dict]:
    """The Form component's children (field components + Footer)."""
    screen = flow_json["screens"][0]
    form = screen["layout"]["children"][0]
    return form["children"]


def test_single_terminal_screen_and_form_scaffold():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    flow_json, _ = build_flow(schema)

    assert flow_json["version"] == "7.0"
    assert len(flow_json["screens"]) == 1
    screen = flow_json["screens"][0]
    assert screen["id"] == "FORM"
    assert screen["terminal"] is True
    assert screen["layout"]["type"] == "SingleColumnLayout"
    form = screen["layout"]["children"][0]
    assert form["type"] == "Form"
    assert form["name"] == "form"


def test_string_maps_to_text_input_with_title_label():
    schema = {"type": "object", "properties": {"note": {"type": "string", "title": "Your note"}}, "required": []}

    flow_json, _ = build_flow(schema)

    field = _children(flow_json)[0]
    assert field == {"type": "TextInput", "name": "note", "label": "Your note", "required": False}


def test_label_falls_back_to_property_name():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    field = _children(build_flow(schema)[0])[0]
    assert field == {"type": "TextInput", "name": "note", "label": "note", "required": True}


def test_string_enum_maps_to_dropdown_with_static_data_source():
    schema = {
        "type": "object",
        "properties": {"pick": {"type": "string", "enum": ["a", "b", "c"]}},
        "required": ["pick"],
    }

    field = _children(build_flow(schema)[0])[0]
    assert field == {
        "type": "Dropdown",
        "name": "pick",
        "label": "pick",
        "required": True,
        "data-source": [{"id": "a", "title": "a"}, {"id": "b", "title": "b"}, {"id": "c", "title": "c"}],
    }


def test_boolean_maps_to_optin():
    schema = {"type": "object", "properties": {"agree": {"type": "boolean"}}, "required": []}

    field = _children(build_flow(schema)[0])[0]
    assert field == {"type": "OptIn", "name": "agree", "label": "agree", "required": False}


@pytest.mark.parametrize("json_type", ["integer", "number"])
def test_integer_and_number_map_to_number_text_input(json_type: str):
    schema = {"type": "object", "properties": {"qty": {"type": json_type}}, "required": ["qty"]}

    field = _children(build_flow(schema)[0])[0]
    assert field == {"type": "TextInput", "name": "qty", "label": "qty", "required": True, "input-type": "number"}


def test_footer_completes_with_form_bindings_for_every_field():
    schema = {
        "type": "object",
        "properties": {"note": {"type": "string"}, "qty": {"type": "integer"}},
        "required": ["note"],
    }

    footer = _children(build_flow(schema)[0])[-1]
    assert footer["type"] == "Footer"
    assert footer["on-click-action"] == {
        "name": "complete",
        "payload": {"note": "${form.note}", "qty": "${form.qty}"},
    }


def test_hash_is_sha256_over_canonical_schema():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    _, schema_hash = build_flow(schema)

    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert schema_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_hash_oracle_matches_production_for_non_ascii_schema():
    # Production dumps with ensure_ascii=False, keeping non-ASCII as UTF-8 bytes
    # rather than \uXXXX escapes — a case an ASCII-only schema cannot expose. The
    # oracle must dump the same way; the escaped form is a different byte string.
    schema = {"type": "object", "properties": {"note": {"type": "string", "title": "café"}}, "required": []}

    _, schema_hash = build_flow(schema)

    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    escaped = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    assert canonical != escaped
    assert schema_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert schema_hash != hashlib.sha256(escaped.encode("utf-8")).hexdigest()


def test_hash_is_stable_across_key_order():
    a = {"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "integer"}}, "required": ["x"]}
    b = {"required": ["x"], "properties": {"y": {"type": "integer"}, "x": {"type": "string"}}, "type": "object"}

    assert build_flow(a)[1] == build_flow(b)[1]


def test_hash_changes_with_schema_content():
    a = {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}
    b = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": []}

    assert build_flow(a)[1] != build_flow(b)[1]


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
        build_flow(schema)


def test_unsupported_property_error_names_the_property():
    schema = {"type": "object", "properties": {"widget": {"type": "object"}}, "required": []}
    with pytest.raises(ChannelInputError, match="'widget'"):
        build_flow(schema)


def test_string_enum_must_be_non_empty_list_of_strings():
    schema = {"type": "object", "properties": {"pick": {"type": "string", "enum": []}}, "required": []}
    with pytest.raises(ChannelInputError, match="'pick'"):
        build_flow(schema)


def test_required_must_be_a_list_of_strings():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": "note"}
    with pytest.raises(ChannelInputError, match="'required'"):
        build_flow(schema)


def test_property_value_must_be_an_object():
    schema = {"type": "object", "properties": {"note": "string"}, "required": []}
    with pytest.raises(ChannelInputError, match="'note'"):
        build_flow(schema)


def test_reserved_flow_token_property_is_refused():
    # ``flow_token`` is Meta's own key on the Flow response; the reply handler strips
    # it, so a field of that name is unanswerable. The mapper refuses it up front —
    # ``build_flow`` is pure and runs before any HTTP, so delivery never reaches the wire.
    schema = {"type": "object", "properties": {"flow_token": {"type": "string"}}, "required": []}
    with pytest.raises(ChannelInputError, match=r"'flow_token'.*reserved"):
        build_flow(schema)


# -- build_form_flow / build_flow_data (per-send, stepped) ---------------------

from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow  # noqa: E402


def _screen_form_children(flow_json: dict, index: int) -> list[dict]:
    return flow_json["screens"][index]["layout"]["children"][0]["children"]


def test_form_flow_one_page_is_one_dynamic_screen():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    flow_json, _ = build_form_flow(schema)

    assert len(flow_json["screens"]) == 1
    screen = flow_json["screens"][0]
    assert screen["id"] == "SCREEN_0"
    assert screen["terminal"] is True
    field = _screen_form_children(flow_json, 0)[0]
    # Every control reads its init-value from the screen data (so a send can prefill it).
    assert field["type"] == "TextInput"
    assert field["init-value"] == "${data.note__init}"
    # A single-screen flow needs no routing model.
    assert "routing_model" not in flow_json


def test_form_flow_one_screen_per_page():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pages = [{"title": "First", "fields": ["a"]}, {"title": "Second", "fields": ["b"]}]

    flow_json, _ = build_form_flow(schema, pages)

    assert [s["id"] for s in flow_json["screens"]] == ["SCREEN_0", "SCREEN_1"]
    assert [s["title"] for s in flow_json["screens"]] == ["First", "Second"]
    assert flow_json["screens"][0]["terminal"] is False
    assert flow_json["screens"][1]["terminal"] is True
    assert flow_json["routing_model"] == {"SCREEN_0": ["SCREEN_1"], "SCREEN_1": []}
    # The terminal screen completes with the flat union of every field.
    footer = _screen_form_children(flow_json, 1)[-1]
    assert footer["on-click-action"]["name"] == "complete"
    assert footer["on-click-action"]["payload"] == {"a": "${data.a__val}", "b": "${form.b}"}
    # The first step navigates forward, carrying its collected value on.
    step_footer = _screen_form_children(flow_json, 0)[-1]
    assert step_footer["on-click-action"]["name"] == "navigate"
    assert step_footer["on-click-action"]["next"] == {"type": "screen", "name": "SCREEN_1"}
    assert step_footer["on-click-action"]["payload"]["a__val"] == "${form.a}"


def test_form_flow_key_differs_when_pages_differ():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    _, key_one_page = build_form_flow(schema)
    _, key_two_pages = build_form_flow(schema, [{"title": "1", "fields": ["a"]}, {"title": "2", "fields": ["b"]}])
    _, key_other_split = build_form_flow(schema, [{"title": "1", "fields": ["a", "b"]}])

    assert key_one_page != key_two_pages
    assert key_two_pages != key_other_split


def test_form_flow_enum_field_reads_a_dynamic_data_source():
    schema = {"type": "object", "properties": {"tier": {"type": "string", "enum": ["gold", "silver"]}}}

    field = _screen_form_children(build_form_flow(schema)[0], 0)[0]

    assert field["type"] == "Dropdown"
    # Dynamic, so a per-send option list can replace the choices without republishing.
    assert field["data-source"] == "${data.tier__ds}"
    assert field["init-value"] == "${data.tier__init}"


def test_form_flow_option_bearing_string_renders_a_dropdown_and_keys_its_own_flow():
    # A plain string property the ask marks option-bearing renders a dynamic Dropdown
    # (parity with web/Slack), and that set joins the publish key: the same schema
    # published with the field option-bearing keys a different Flow than the enum-only
    # publish (``option_fields`` empty), so a reused schema with new option-bearing set
    # publishes its own Flow.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}

    plain_json, enum_only_key = build_form_flow(schema)
    option_json, option_key = build_form_flow(schema, None, {"note"})

    assert _screen_form_children(plain_json, 0)[0]["type"] == "TextInput"
    dropdown = _screen_form_children(option_json, 0)[0]
    assert dropdown["type"] == "Dropdown"
    assert dropdown["data-source"] == "${data.note__ds}"
    assert dropdown["init-value"] == "${data.note__init}"
    assert option_key != enum_only_key


def test_form_flow_reuses_the_key_for_an_unchanged_triple():
    # The same (schema, pages, option_fields) triple reuses one published Flow.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    _, first = build_form_flow(schema, None, {"note"})
    _, second = build_form_flow(schema, None, {"note"})
    assert first == second


def test_form_flow_unknown_page_field_raises():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pytest.raises(ChannelInputError, match="ghost"):
        build_form_flow(schema, [{"title": "P", "fields": ["ghost"]}])


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
