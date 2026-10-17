"""The channel-deliverable form-schema subset — ``validate_channel_form_schema``
and the shared ``channel_form_fields`` walk that both ``ask`` and the callback
form renderer use as the ONE definition of the subset.
"""

from __future__ import annotations

import re

import jsonschema
import pytest
from pydantic import BaseModel
from tai42_contract.interactions import FormShapeKind

from tai42_skeleton.interactions.form_schema import (
    channel_form_fields,
    date_constraint_errors,
    date_constraint_mismatch,
    effective_answer_schema,
    validate_channel_form_schema,
    validate_form_answer_schema,
)


def test_effective_answer_schema_replaces_enum_with_per_send_options():
    schema = {"type": "object", "properties": {"color": {"type": "string", "enum": ["red", "blue"]}}}
    data = {"options": {"color": [{"value": "green"}, {"value": "amber", "label": "Amber"}]}}
    effective = effective_answer_schema(schema, data)
    assert effective["properties"]["color"]["enum"] == ["green", "amber"]
    # The original schema is not mutated.
    assert schema["properties"]["color"]["enum"] == ["red", "blue"]


def test_effective_answer_schema_applies_array_options_to_items():
    # For a multi-select (array) property the per-send choices belong on ``items``,
    # not the array itself — an ``enum`` on the array would demand the whole submitted
    # list equal one option, rejecting every real multi-select answer.
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    }
    data = {"options": {"tags": [{"value": "a"}, {"value": "b", "label": "Bee"}]}}
    effective = effective_answer_schema(schema, data)
    prop = effective["properties"]["tags"]
    assert "enum" not in prop
    assert prop["items"] == {"type": "string", "enum": ["a", "b"]}
    # The original schema is not mutated.
    assert schema["properties"]["tags"]["items"] == {"type": "string"}


def test_effective_answer_schema_no_options_returns_schema_unchanged():
    schema = {"type": "object", "properties": {"color": {"type": "string"}}}
    assert effective_answer_schema(schema, None) is schema
    assert effective_answer_schema(schema, {"values": {"color": "x"}}) is schema


class _OptForm(BaseModel):
    # ``str | None`` renders as ``anyOf`` (no scalar ``type``) in JSON schema — a
    # nullable/optional pydantic field is outside the subset.
    name: str | None = None


def _valid_subset() -> dict:
    return {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "title": "Name"},
            "color": {"type": "string", "enum": ["red", "blue"]},
            "agree": {"type": "boolean"},
            "count": {"type": "integer"},
            "score": {"type": "number"},
        },
    }


def test_accepts_the_full_valid_subset():
    schema = _valid_subset()
    assert validate_channel_form_schema(schema) is None
    fields = channel_form_fields(schema)
    assert [(name, is_required) for name, _prop, is_required in fields] == [
        ("name", True),
        ("color", False),
        ("agree", False),
        ("count", False),
        ("score", False),
    ]


def test_admits_a_string_description_as_the_field_second_line():
    # The JSON-Schema ``description`` is the field's optional second line; a string (including an
    # empty string, which pydantic-declared schemas emit for an empty field doc) is admitted.
    assert (
        validate_channel_form_schema(
            {"type": "object", "properties": {"x": {"type": "string", "description": "a hint"}}}
        )
        is None
    )
    assert (
        validate_channel_form_schema({"type": "object", "properties": {"x": {"type": "string", "description": ""}}})
        is None
    )


def test_rejects_a_non_string_description():
    with pytest.raises(ValueError, match="description must be a string when present"):
        validate_channel_form_schema({"type": "object", "properties": {"x": {"type": "string", "description": 7}}})


def test_rejects_non_object_root():
    with pytest.raises(ValueError, match="top-level type must be 'object'"):
        validate_channel_form_schema({"type": "array", "properties": {"x": {"type": "string"}}})


def test_rejects_empty_properties():
    with pytest.raises(ValueError, match="non-empty object 'properties'"):
        validate_channel_form_schema({"type": "object", "properties": {}})


def test_rejects_missing_properties():
    with pytest.raises(ValueError, match="non-empty object 'properties'"):
        validate_channel_form_schema({"type": "object"})


def test_admits_array_of_strings_property():
    # An array whose items are strings is a renderable multiple-choice field (resolves the
    # array-dead-on-channel asymmetry); it passes the subset.
    validate_channel_form_schema(
        {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    )


def test_admits_array_of_strings_with_enum():
    validate_channel_form_schema(
        {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}}}
    )


def test_rejects_array_without_items():
    with pytest.raises(ValueError, match="property 'tags' is an array but declares no object 'items'"):
        validate_channel_form_schema({"type": "object", "properties": {"tags": {"type": "array"}}})


def test_rejects_array_of_non_strings():
    with pytest.raises(ValueError, match="property 'nums' is an array whose items are not strings"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"nums": {"type": "array", "items": {"type": "integer"}}}}
        )


def test_rejects_nested_object_property():
    with pytest.raises(ValueError, match="property 'sub' has type 'object'"):
        validate_channel_form_schema({"type": "object", "properties": {"sub": {"type": "object", "properties": {}}}})


def test_rejects_missing_type_property():
    # A pydantic ``str | None`` -> ``anyOf`` with no scalar ``type``.
    with pytest.raises(ValueError, match="property 'name' has type None"):
        validate_channel_form_schema(_OptForm.model_json_schema())


def test_rejects_ref_property():
    with pytest.raises(ValueError, match="property 'sub' has type None"):
        validate_channel_form_schema({"type": "object", "properties": {"sub": {"$ref": "#/$defs/Sub"}}})


def test_rejects_enum_on_non_string():
    with pytest.raises(ValueError, match="property 'n' has an enum but is not a 'string'"):
        validate_channel_form_schema({"type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}}})


def test_rejects_empty_enum():
    with pytest.raises(ValueError, match="property 'c' enum must be a non-empty list"):
        validate_channel_form_schema({"type": "object", "properties": {"c": {"type": "string", "enum": []}}})


def test_rejects_non_string_enum_values():
    with pytest.raises(ValueError, match="property 'c' enum must contain only strings"):
        validate_channel_form_schema({"type": "object", "properties": {"c": {"type": "string", "enum": ["a", 2]}}})


def test_rejects_required_naming_undeclared_property():
    with pytest.raises(ValueError, match="'required' names undeclared properties: \\['ghost'\\]"):
        validate_channel_form_schema({"type": "object", "required": ["ghost"], "properties": {"x": {"type": "string"}}})


def test_rejects_non_list_required():
    with pytest.raises(ValueError, match="'required' must be a list"):
        validate_channel_form_schema({"type": "object", "required": "x", "properties": {"x": {"type": "string"}}})


def test_rejects_non_object_property():
    with pytest.raises(ValueError, match="property 'x' must be an object"):
        validate_channel_form_schema({"type": "object", "properties": {"x": "string"}})


@pytest.mark.parametrize("fmt", ["date", "time", "date-time", "email"])
def test_accepts_the_allowed_string_formats(fmt):
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": fmt}}}
    assert validate_channel_form_schema(schema) is None


def test_rejects_an_unsupported_format():
    with pytest.raises(ValueError, match="property 'when' has unsupported format 'duration'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"when": {"type": "string", "format": "duration"}}}
        )


def test_rejects_format_on_a_non_string_property():
    with pytest.raises(ValueError, match="property 'n' has format 'date' but is not a 'string'"):
        validate_channel_form_schema({"type": "object", "properties": {"n": {"type": "integer", "format": "date"}}})


def test_accepts_format_alongside_an_enum():
    # A dated enum is still an enum: the choice control is rendered and the format is
    # enforced on the answer, so the two coexist rather than one refusing the other.
    schema = {"type": "object", "properties": {"day": {"type": "string", "enum": ["2024-01-01"], "format": "date"}}}
    assert validate_channel_form_schema(schema) is None


# === inputMode (the keyboard/input-kind hint on a string field) =============


@pytest.mark.parametrize("mode", ["text", "numeric", "decimal", "tel", "email", "url"])
def test_accepts_each_input_mode(mode):
    schema = {"type": "object", "properties": {"ref": {"type": "string", "inputMode": mode}}}
    assert validate_channel_form_schema(schema) is None


def test_rejects_unknown_input_mode():
    with pytest.raises(ValueError, match="property 'ref' has unsupported inputMode 'phone'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"ref": {"type": "string", "inputMode": "phone"}}}
        )


def test_rejects_non_string_input_mode_value():
    with pytest.raises(ValueError, match="property 'ref' has unsupported inputMode 5"):
        validate_channel_form_schema({"type": "object", "properties": {"ref": {"type": "string", "inputMode": 5}}})


def test_rejects_input_mode_on_a_non_string_property():
    with pytest.raises(ValueError, match="property 'n' has inputMode 'numeric' but is not a 'string'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"n": {"type": "integer", "inputMode": "numeric"}}}
        )


def test_rejects_input_mode_on_an_array_property():
    # The keyword must be refused on an array itself, not only inside its items — otherwise it
    # would ride through silently on the array node.
    with pytest.raises(ValueError, match="property 'tags' has inputMode 'numeric' but is not a 'string'"):
        validate_channel_form_schema(
            {
                "type": "object",
                "properties": {"tags": {"type": "array", "inputMode": "numeric", "items": {"type": "string"}}},
            }
        )


def test_rejects_input_mode_alongside_an_enum():
    with pytest.raises(ValueError, match="property 'c' carries inputMode alongside an enum"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"c": {"type": "string", "enum": ["a", "b"], "inputMode": "email"}}}
        )


def test_rejects_input_mode_alongside_a_format():
    with pytest.raises(ValueError, match="property 'd' carries inputMode alongside format 'date'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"d": {"type": "string", "format": "date", "inputMode": "numeric"}}}
        )


def test_rejects_input_mode_alongside_the_email_format():
    # The email control IS the email keyboard on every surface, so the hint names nothing more.
    with pytest.raises(ValueError, match="property 'e' carries inputMode alongside format 'email'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"e": {"type": "string", "format": "email", "inputMode": "email"}}}
        )


def test_rejects_input_mode_inside_array_items():
    with pytest.raises(ValueError, match="property 'tags' items carry inputMode"):
        validate_channel_form_schema(
            {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": {"type": "string", "inputMode": "email"}}},
            }
        )


# === control (chips / textarea) ============================================


def test_accepts_chips_on_a_string_enum():
    schema = {"type": "object", "properties": {"c": {"type": "string", "enum": ["a", "b"], "control": "chips"}}}
    assert validate_channel_form_schema(schema) is None


def test_accepts_chips_on_an_array_with_items_enum():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "control": "chips"}},
    }
    assert validate_channel_form_schema(schema) is None


def test_accepts_textarea_on_a_plain_string():
    schema = {"type": "object", "properties": {"notes": {"type": "string", "control": "textarea"}}}
    assert validate_channel_form_schema(schema) is None


def test_rejects_unknown_control():
    with pytest.raises(ValueError, match="property 'c' has unsupported control 'pills'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"c": {"type": "string", "enum": ["a"], "control": "pills"}}}
        )


def test_rejects_non_string_control_value():
    with pytest.raises(ValueError, match="property 'c' has unsupported control 5"):
        validate_channel_form_schema({"type": "object", "properties": {"c": {"type": "string", "control": 5}}})


def test_rejects_chips_on_a_non_choice_property():
    with pytest.raises(ValueError, match="property 'n' has control 'chips' but is not a string or an array of strings"):
        validate_channel_form_schema({"type": "object", "properties": {"n": {"type": "integer", "control": "chips"}}})


def test_rejects_chips_without_an_enum():
    with pytest.raises(ValueError, match="property 'c' has control 'chips' but declares no enum"):
        validate_channel_form_schema({"type": "object", "properties": {"c": {"type": "string", "control": "chips"}}})


def test_rejects_chips_on_an_array_without_items_enum():
    with pytest.raises(ValueError, match="property 'tags' has control 'chips' but declares no enum"):
        validate_channel_form_schema(
            {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": {"type": "string"}, "control": "chips"}},
            }
        )


def test_rejects_control_inside_array_items():
    with pytest.raises(ValueError, match="property 'tags' items carry control"):
        validate_channel_form_schema(
            {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": {"type": "string", "control": "chips"}}},
            }
        )


def test_rejects_textarea_on_a_non_string_property():
    with pytest.raises(ValueError, match="property 'n' has control 'textarea' but is not a 'string'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"n": {"type": "integer", "control": "textarea"}}}
        )


def test_rejects_textarea_alongside_an_enum():
    with pytest.raises(ValueError, match="property 'c' carries control 'textarea' alongside an enum"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"c": {"type": "string", "enum": ["a"], "control": "textarea"}}}
        )


def test_rejects_textarea_alongside_a_format():
    with pytest.raises(ValueError, match="property 'd' carries control 'textarea' alongside format 'date'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"d": {"type": "string", "format": "date", "control": "textarea"}}}
        )


def test_rejects_textarea_alongside_an_input_mode():
    with pytest.raises(ValueError, match="property 'd' carries control 'textarea' alongside inputMode 'numeric'"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"d": {"type": "string", "inputMode": "numeric", "control": "textarea"}}}
        )


# === minLength / maxLength shape =============================================


def test_rejects_min_length_above_max_length():
    with pytest.raises(ValueError, match="property 'bio' has minLength 10 above maxLength 5"):
        validate_channel_form_schema(
            {"type": "object", "properties": {"bio": {"type": "string", "minLength": 10, "maxLength": 5}}}
        )


# === number and selection-count bounds no answer can meet ====================


@pytest.mark.parametrize(
    ("prop", "words"),
    [
        ({"type": "integer", "minimum": 5, "maximum": 1}, "no whole number can meet (minimum 5, maximum 1)"),
        ({"type": "integer", "minimum": 1.2, "maximum": 1.8}, "no whole number can meet (minimum 1.2, maximum 1.8)"),
        (
            {"type": "integer", "exclusiveMinimum": 1, "exclusiveMaximum": 2},
            "no whole number can meet (exclusiveMinimum 1, exclusiveMaximum 2)",
        ),
        ({"type": "number", "minimum": 5, "maximum": 1}, "no number can meet (minimum 5, maximum 1)"),
        (
            {"type": "number", "minimum": 1, "exclusiveMaximum": 1},
            "no number can meet (minimum 1, exclusiveMaximum 1)",
        ),
    ],
)
def test_rejects_number_bounds_no_value_can_meet(prop, words):
    with pytest.raises(ValueError, match=re.escape(f"form schema property 'qty' has bounds {words}")):
        validate_channel_form_schema({"type": "object", "properties": {"qty": prop}})


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "integer", "minimum": 1.5, "maximum": 2},
        {"type": "integer", "minimum": 2, "maximum": 2},
        {"type": "integer", "exclusiveMinimum": 1, "maximum": 2},
        {"type": "number", "minimum": 1.2, "maximum": 1.8},
        {"type": "number", "minimum": 1, "maximum": 1},
        {"type": "number", "exclusiveMinimum": 1, "exclusiveMaximum": 1.5},
    ],
)
def test_admits_number_bounds_some_value_meets(prop):
    validate_channel_form_schema({"type": "object", "properties": {"qty": prop}})


def test_rejects_min_items_above_max_items():
    prop = {"type": "array", "items": {"type": "string", "enum": ["a", "b", "c"]}, "minItems": 3, "maxItems": 2}
    with pytest.raises(ValueError, match="property 'picks' has minItems 3 above maxItems 2"):
        validate_channel_form_schema({"type": "object", "properties": {"picks": prop}})


def test_admits_min_items_within_max_items():
    prop = {"type": "array", "items": {"type": "string", "enum": ["a", "b", "c"]}, "minItems": 2, "maxItems": 2}
    validate_channel_form_schema({"type": "object", "properties": {"picks": prop}})


def test_admits_min_length_within_max_length():
    validate_channel_form_schema(
        {"type": "object", "properties": {"bio": {"type": "string", "minLength": 2, "maxLength": 200}}}
    )


# === schemas no answer could be checked against ==============================


@pytest.mark.parametrize(
    ("prop", "words"),
    [
        ({"type": "integer", "minimum": True}, "True is not of type 'number'"),
        ({"type": "integer", "minimum": "1"}, "'1' is not of type 'number'"),
        ({"type": "integer", "maximum": None}, "None is not of type 'number'"),
        ({"type": "string", "minLength": -1}, "-1 is less than the minimum of 0"),
        ({"type": "string", "pattern": "("}, "'(' is not a 'regex'"),
    ],
)
def test_rejects_a_property_breaking_the_json_schema_rules(prop, words):
    with pytest.raises(
        ValueError, match=re.escape(f"form schema property 'qty' breaks the JSON Schema rules: {words}")
    ):
        validate_channel_form_schema({"type": "object", "properties": {"qty": prop}})


@pytest.mark.parametrize(
    ("pattern", "fault"),
    [
        ("a{4294967296}", "the repetition number is too large"),
        ("(" * 2000 + ")" * 2000, "maximum recursion depth exceeded"),
    ],
)
def test_rejects_a_pattern_the_regex_engine_cannot_compile(pattern, fault):
    with pytest.raises(
        ValueError, match=re.escape("form schema property 's' breaks the JSON Schema rules: ")
    ) as caught:
        validate_channel_form_schema({"type": "object", "properties": {"s": {"type": "string", "pattern": pattern}}})
    rules_fault = caught.value.__cause__
    assert isinstance(rules_fault, jsonschema.SchemaError)
    assert fault in str(rules_fault.cause)


@pytest.mark.parametrize("ptype", ["integer", "number"])
@pytest.mark.parametrize("key", ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_rejects_a_bound_that_is_not_a_finite_number(ptype, key, value):
    with pytest.raises(
        ValueError,
        match=re.escape(f"form schema property 'qty' has a value at {key} that is not a finite number ({value})"),
    ):
        validate_channel_form_schema({"type": "object", "properties": {"qty": {"type": ptype, key: value}}})


@pytest.mark.parametrize(
    ("prop", "where", "value"),
    [
        ({"type": "string", "enum": ["a", float("nan")]}, "at enum/1", float("nan")),
        ({"type": "number", "default": float("inf")}, "at default", float("inf")),
        ({"type": "array", "items": {"type": "number", "maximum": float("-inf")}}, "at items/maximum", float("-inf")),
    ],
)
def test_rejects_a_number_that_is_not_finite_anywhere_in_a_property(prop, where, value):
    with pytest.raises(
        ValueError,
        match=re.escape(f"form schema property 'qty' has a value {where} that is not a finite number ({value})"),
    ):
        validate_channel_form_schema({"type": "object", "properties": {"qty": prop}})


def test_rejects_a_number_that_is_not_finite_outside_the_properties():
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}, "$defs": {"n": {"const": float("nan")}}}
    with pytest.raises(
        ValueError, match=re.escape("form schema has a value at $defs/n/const that is not a finite number (nan)")
    ):
        validate_channel_form_schema(schema)


def test_rejects_a_schema_that_holds_itself():
    schema: dict = {"type": "object", "properties": {"qty": {"type": "array"}}}
    schema["properties"]["qty"]["items"] = schema["properties"]["qty"]
    with pytest.raises(
        ValueError,
        match=re.escape(
            "form schema property 'qty' has a value at items that loops back to a value it sits inside; "
            "a schema must be plain JSON, which has no cycles"
        ),
    ):
        validate_channel_form_schema(schema)


def test_admits_a_value_shared_by_two_places_in_the_schema():
    shared = {"type": "string"}
    validate_channel_form_schema({"type": "object", "properties": {"a": shared, "b": shared}})


# === date constraint keys (send-time shape check) ===========================


def _date_prop(**extra):
    return {"type": "object", "properties": {"d": {"type": "string", "format": "date", **extra}}}


def test_admits_date_bounds_and_unavailable():
    validate_channel_form_schema(_date_prop(minDate="2024-01-01", maxDate="2024-12-31"))
    validate_channel_form_schema(_date_prop(unavailableDates=["2024-07-04", "saturday", "sunday"]))


def test_rejects_date_bound_on_non_date_property():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minDate": "2024-01-01"}}}
    with pytest.raises(ValueError, match="carries date constraints"):
        validate_channel_form_schema(schema)


def test_rejects_bad_bound_date():
    with pytest.raises(ValueError, match="minDate must be a YYYY-MM-DD date"):
        validate_channel_form_schema(_date_prop(minDate="01/01/2024"))


def test_rejects_max_before_min():
    with pytest.raises(ValueError, match=r"maxDate .* before minDate"):
        validate_channel_form_schema(_date_prop(minDate="2024-12-31", maxDate="2024-01-01"))


def test_rejects_bad_unavailable_entry():
    with pytest.raises(ValueError, match="unavailableDates entries"):
        validate_channel_form_schema(_date_prop(unavailableDates=["funday"]))


# === date RANGE as two date fields ==========================================


def _range_schema(**end_extra):
    return {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", **end_extra},
        },
    }


def test_admits_range_pairing():
    validate_channel_form_schema(_range_schema(minDays=1, maxDays=30))


def test_rejects_range_start_unknown_property():
    schema = {"type": "object", "properties": {"end": {"type": "string", "format": "date", "rangeStart": "ghost"}}}
    with pytest.raises(ValueError, match="rangeStart names undeclared property 'ghost'"):
        validate_channel_form_schema(schema)


def test_rejects_range_start_non_date_property():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
        },
    }
    with pytest.raises(ValueError, match="rangeStart names non-date property 'start'"):
        validate_channel_form_schema(schema)


def test_rejects_span_key_without_range_start():
    with pytest.raises(ValueError, match="carries minDays but declares no rangeStart"):
        validate_channel_form_schema(_date_prop(minDays=1))


def test_rejects_min_days_above_max_days():
    with pytest.raises(ValueError, match="minDays 5 above maxDays 2"):
        validate_channel_form_schema(_range_schema(minDays=5, maxDays=2))


def test_rejects_chained_range_start():
    # A start that is itself an end (a.rangeStart=b, b.rangeStart=c) makes "the pair" ambiguous.
    schema = {
        "type": "object",
        "properties": {
            "c": {"type": "string", "format": "date"},
            "b": {"type": "string", "format": "date", "rangeStart": "c"},
            "a": {"type": "string", "format": "date", "rangeStart": "b"},
        },
    }
    with pytest.raises(ValueError, match="property 'a' rangeStart names 'b', which is already paired with 'c'"):
        validate_channel_form_schema(schema)


def test_rejects_two_ends_naming_the_same_start():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end1": {"type": "string", "format": "date", "rangeStart": "start"},
            "end2": {"type": "string", "format": "date", "rangeStart": "start"},
        },
    }
    with pytest.raises(
        ValueError, match="property 'end2' rangeStart names 'start', which is already paired with 'end1'"
    ):
        validate_channel_form_schema(schema)


# === visibleWhen predicate ==================================================


def _vw_schema(predicate):
    return {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "detail": {"type": "string", "visibleWhen": predicate},
        },
    }


def test_admits_visible_when():
    validate_channel_form_schema(_vw_schema({"field": "mode", "equals": "a"}))
    validate_channel_form_schema(_vw_schema({"field": "mode", "in": ["a", "b"]}))
    validate_channel_form_schema(_vw_schema({"field": "mode", "notEmpty": True}))


def test_rejects_visible_when_unknown_field():
    with pytest.raises(ValueError, match="visibleWhen names undeclared property 'ghost'"):
        validate_channel_form_schema(_vw_schema({"field": "ghost", "equals": "a"}))


def test_rejects_visible_when_two_operators():
    with pytest.raises(ValueError, match="exactly one of"):
        validate_channel_form_schema(_vw_schema({"field": "mode", "equals": "a", "notEmpty": True}))


def test_rejects_visible_when_self_reference():
    schema = {"type": "object", "properties": {"x": {"type": "string", "visibleWhen": {"field": "x", "equals": "a"}}}}
    with pytest.raises(ValueError, match="cannot reference itself"):
        validate_channel_form_schema(schema)


# === effective_answer_schema with reaction-fed choices ======================


def test_effective_schema_omits_enum_for_a_choice_field():
    schema = {"type": "object", "properties": {"slot": {"type": "string", "enum": ["9am", "10am"]}}}
    out = effective_answer_schema(schema, None, choices=["slot"])
    assert "enum" not in out["properties"]["slot"]


def test_effective_schema_omits_items_enum_for_an_array_choice_field():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a"]}}}}
    out = effective_answer_schema(schema, None, choices=["tags"])
    assert "enum" not in out["properties"]["tags"]["items"]


def test_effective_schema_skips_per_send_options_for_a_choice_field():
    schema = {"type": "object", "properties": {"slot": {"type": "string", "enum": ["9am"]}}}
    data = {"options": {"slot": [{"value": "11am"}]}}
    out = effective_answer_schema(schema, data, choices=["slot"])
    # The per-send list is NOT stamped and the static enum is omitted — type only.
    assert "enum" not in out["properties"]["slot"]


# === submit-time date enforcement ===========================================


def _field(result) -> str | None:
    return result[1] if result is not None else None


def test_date_constraint_mismatch_bounds_and_unavailable():
    schema = _date_prop(minDate="2024-01-01", maxDate="2024-12-31", unavailableDates=["2024-07-04", "sunday"])
    assert date_constraint_mismatch(schema, {"d": "2024-06-15"}) is None  # a Saturday in range
    assert _field(date_constraint_mismatch(schema, {"d": "2023-12-31"})) == "d"  # before min
    assert _field(date_constraint_mismatch(schema, {"d": "2025-01-01"})) == "d"  # after max
    assert _field(date_constraint_mismatch(schema, {"d": "2024-07-04"})) == "d"  # explicit unavailable
    assert _field(date_constraint_mismatch(schema, {"d": "2024-07-07"})) == "d"  # a Sunday


def test_date_constraint_mismatch_range_order_and_span():
    schema = _range_schema(minDays=2, maxDays=3)
    assert date_constraint_mismatch(schema, {"start": "2024-01-01", "end": "2024-01-02"}) is None  # 2 inclusive days
    assert _field(date_constraint_mismatch(schema, {"start": "2024-01-05", "end": "2024-01-01"})) == "end"
    assert _field(date_constraint_mismatch(schema, {"start": "2024-01-01", "end": "2024-01-01"})) == "end"  # 1 day < 2
    assert _field(date_constraint_mismatch(schema, {"start": "2024-01-01", "end": "2024-01-10"})) == "end"  # 10 > 3


def _date_errors(schema, answer) -> list[tuple[str, FormShapeKind, dict]]:
    return list(date_constraint_errors(schema, answer))


def test_date_constraint_errors_bounds_and_unavailable():
    schema = _date_prop(minDate="2024-01-01", maxDate="2024-12-31", unavailableDates=["2024-07-04", "sunday"])
    assert _date_errors(schema, {"d": "2024-06-15"}) == []  # a Saturday in range
    assert _date_errors(schema, {"d": "2023-12-31"}) == [("d", FormShapeKind.MIN_DATE, {"minDate": "2024-01-01"})]
    assert _date_errors(schema, {"d": "2025-01-01"}) == [("d", FormShapeKind.MAX_DATE, {"maxDate": "2024-12-31"})]
    assert _date_errors(schema, {"d": "2024-07-04"}) == [("d", FormShapeKind.DATE_UNAVAILABLE, {"date": "2024-07-04"})]
    assert _date_errors(schema, {"d": "2024-07-07"}) == [("d", FormShapeKind.DATE_UNAVAILABLE, {"date": "2024-07-07"})]


def test_date_constraint_errors_range_order_and_span():
    schema = _range_schema(minDays=2, maxDays=3)
    assert _date_errors(schema, {"start": "2024-01-01", "end": "2024-01-02"}) == []  # 2 inclusive days
    assert _date_errors(schema, {"start": "2024-01-05", "end": "2024-01-01"}) == [
        ("end", FormShapeKind.RANGE_ORDER, {"rangeStart": "start"})
    ]
    span = {"minDays": 2, "maxDays": 3}
    assert _date_errors(schema, {"start": "2024-01-01", "end": "2024-01-01"}) == [
        ("end", FormShapeKind.RANGE_SPAN, {**span, "days": 1})
    ]
    assert _date_errors(schema, {"start": "2024-01-01", "end": "2024-01-10"}) == [
        ("end", FormShapeKind.RANGE_SPAN, {**span, "days": 10})
    ]


def test_date_constraint_errors_yield_every_failing_date_field():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "format": "date", "minDate": "2024-01-01"},
            "b": {"type": "string", "format": "date", "maxDate": "2024-01-01"},
            "c": {"type": "string", "format": "date"},
        },
    }
    answer = {"a": "2023-01-01", "b": "2025-01-01", "c": "not-a-date"}
    assert [field for field, _, _ in _date_errors(schema, answer)] == ["a", "b"]


def test_channel_form_subset_refuses_a_property_named_proto():
    schema = {"type": "object", "properties": {"__proto__": {"type": "string"}, "name": {"type": "string"}}}
    with pytest.raises(ValueError, match=re.escape("form schema property '__proto__' is not an allowed name")):
        validate_channel_form_schema(schema)
    with pytest.raises(ValueError, match=re.escape("form schema property '__proto__' is not an allowed name")):
        validate_form_answer_schema(schema)
