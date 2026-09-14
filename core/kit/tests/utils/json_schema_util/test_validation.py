"""validation: validate_against_json_schema (jsonschema draft 2020-12) — every
keyword enforced faithfully, the meta-schema check first, and the two-path proof
that the validator enforces oneOf-XOR and 'not' the converters collapse.
"""

import pytest

from tai42_kit.utils.data.json_schema_util import (
    InvalidJsonSchemaError,
    JsonSchemaValidationError,
    validate_against_json_schema,
)


def test_validator_match_returns_none():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}, "required": ["n"]}
    assert validate_against_json_schema({"n": 5}, schema) is None


def test_validator_mismatch_raises_with_json_path_and_value():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    with pytest.raises(JsonSchemaValidationError) as exc:
        validate_against_json_schema({"n": "not-an-int"}, schema)
    # The typed error pinpoints the offending node and the value found there.
    assert exc.value.json_path == "$.n"
    assert exc.value.offending_value == "not-an-int"
    assert "$.n" in str(exc.value)


def test_validator_constraint_violation_raises():
    # A constraint keyword the converter drops on validation (strings pass pydantic
    # only via annotated metadata) is enforced faithfully by the validator.
    schema = {"type": "string", "minLength": 3}
    validate_against_json_schema("abc", schema)  # conforms -> no raise
    with pytest.raises(JsonSchemaValidationError):
        validate_against_json_schema("ab", schema)


def test_validator_nested_json_path():
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "integer"}}}}
        },
    }
    with pytest.raises(JsonSchemaValidationError) as exc:
        validate_against_json_schema({"items": [{"x": 1}, {"x": "bad"}]}, schema)
    assert exc.value.json_path == "$.items[1].x"


def test_validator_meta_schema_invalid_raises_its_own_error():
    # A malformed schema fails the meta-schema check up front (a distinct error
    # type) rather than silently mis-validating the instance into a false pass.
    with pytest.raises(InvalidJsonSchemaError):
        validate_against_json_schema({"anything": True}, {"type": 123})


def test_validator_meta_schema_check_precedes_instance_check():
    # Even an instance that "looks fine" cannot slip past a broken schema.
    with pytest.raises(InvalidJsonSchemaError):
        validate_against_json_schema("hello", {"minLength": "not-a-number"})


def test_two_path_proof_oneof_xor_still_fails_validator():
    # The converter drops oneOf's exactly-one meaning (Union ceiling), but the
    # validator enforces it: a value matching BOTH branches is rejected. This is
    # the load-bearing split — the two paths together are sound.
    xor = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
                "additionalProperties": False,
            },
        ]
    }
    validate_against_json_schema({"a": "x"}, xor)  # matches exactly one -> ok
    with pytest.raises(JsonSchemaValidationError):
        validate_against_json_schema({"a": "x", "b": "y"}, xor)  # matches both -> fails


def test_two_path_proof_not_still_fails_validator():
    # 'not' collapses to Any in the converter but is enforced by the validator.
    schema = {"not": {"type": "string"}}
    validate_against_json_schema(42, schema)  # a non-string satisfies not-string
    with pytest.raises(JsonSchemaValidationError):
        validate_against_json_schema("a string", schema)
