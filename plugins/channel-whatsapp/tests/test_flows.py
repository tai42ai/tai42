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
