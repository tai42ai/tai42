"""validation: validate_against_json_schema (jsonschema draft 2020-12) — every
keyword enforced faithfully, the meta-schema check first, and the two-path proof
that the validator enforces oneOf-XOR and 'not' the converters collapse.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tai42_kit.utils.data.json_schema_util import (
    InvalidJsonSchemaError,
    JsonSchemaValidationError,
    check_json_schema,
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


@pytest.fixture
def schema_server():
    """A local schema server: its base URL and the paths it was asked for."""
    asked: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            asked.append(self.path)
            body = json.dumps({"type": "integer"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", asked
    server.shutdown()
    thread.join()
    server.server_close()


def test_validator_refuses_a_reference_outside_the_schema_and_fetches_nothing(schema_server):
    url, asked = schema_server
    schema = {"type": "object", "properties": {"a": {"$ref": f"{url}/a.json"}}}
    refusal = f"JSON schema property 'a' breaks the JSON Schema rules: $ref '{url}/a.json' does not resolve"
    with pytest.raises(InvalidJsonSchemaError, match=re.escape(refusal)):
        validate_against_json_schema({"a": 1}, schema)
    assert asked == []


def test_validator_follows_a_reference_inside_the_schema():
    schema = {"$defs": {"n": {"type": "integer"}}, "type": "object", "properties": {"a": {"$ref": "#/$defs/n"}}}
    validate_against_json_schema({"a": 1}, schema)
    with pytest.raises(JsonSchemaValidationError, match=r"value does not match schema at \$\.a"):
        validate_against_json_schema({"a": "x"}, schema)


_CYCLE = (
    "forms a cycle of references that never descends into the instance, so checking an instance that follows it "
    "would never end"
)
_META_ANCHOR = "https://json-schema.org/draft/2020-12/schema#meta"
_M20 = "https://json-schema.org/draft/2020-12/schema"
_APPLICATOR = "https://json-schema.org/draft/2020-12/meta/applicator"
_CORE = "https://json-schema.org/draft/2020-12/meta/core"
_X_TO_Y = {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}}
_ENTERED = (
    "JSON schema breaks the JSON Schema rules at $defs/x/$dynamicAnchor: $dynamicAnchor 'meta' below the root "
    "is not allowed when the schema has an $id and refers into a JSON Schema meta-schema: "
    f"{{keyword}} '{_META_ANCHOR}' names the meta-schema's $dynamicAnchor 'meta', which would re-enter the schema "
    "here and resolve its references against the meta-schema — rename the anchor"
)


def _entering(keyword: str, defs: dict) -> dict:
    # A property naming the meta-schema's own dynamic anchor, beside an anchor of that name below a
    # root ``$id``: the lookup enters the meta-schema and comes back into the schema at ``$defs/x``.
    return {"$id": "https://e.com/r", "type": "object", "properties": {"m": {keyword: _META_ANCHOR}}, "$defs": defs}


@pytest.mark.parametrize(
    ("schema", "refusal"),
    [
        (
            {"properties": {"a": {"$ref": "#/nope"}}},
            "JSON schema property 'a' breaks the JSON Schema rules: $ref '#/nope' does not resolve: nothing at '/nope'",
        ),
        (
            {"properties": {"a": {"$ref": "#/properties/b/type"}, "b": {"type": "string"}}},
            "JSON schema property 'a' breaks the JSON Schema rules: "
            "$ref '#/properties/b/type' points at a value that is not a schema",
        ),
        ({"$ref": "#"}, f"JSON schema breaks the JSON Schema rules at $ref: $ref '#' {_CYCLE}"),
        (
            {"anyOf": [{"type": "string"}, {"$ref": "#"}]},
            f"JSON schema breaks the JSON Schema rules at anyOf/1/$ref: $ref '#' {_CYCLE}",
        ),
        (
            {
                "$id": "https://e.com/r",
                "type": "object",
                "properties": {"m": {"$ref": "https://json-schema.org/draft/2020-12/schema"}},
                "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
            },
            "JSON schema breaks the JSON Schema rules at $defs/x/$dynamicAnchor: $dynamicAnchor 'meta' below the root "
            "is not allowed when the schema has an $id and refers into a JSON Schema meta-schema: the meta-schema's "
            "$dynamicRef '#meta' would re-enter the schema here and resolve its references against the meta-schema "
            "— rename the anchor",
        ),
        (
            _entering("$ref", {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}}),
            _ENTERED.format(keyword="$ref"),
        ),
        (
            _entering("$dynamicRef", {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}}),
            _ENTERED.format(keyword="$dynamicRef"),
        ),
        (_entering("$ref", {"x": {"$dynamicAnchor": "meta", "type": "object"}}), _ENTERED.format(keyword="$ref")),
        (
            {
                "type": "object",
                "properties": {"a": {"anyOf": [{"type": "string"}]}, "b": {"$ref": "#/properties/a/anyOf/-1"}},
            },
            "JSON schema property 'b' breaks the JSON Schema rules: $ref '#/properties/a/anyOf/-1' indexes an array "
            "with '-1', which is not a JSON Pointer array index: write the index as a whole number like 0 or 2",
        ),
        (
            {"$id": _APPLICATOR, "type": "object", "properties": {"m": {"$ref": _M20}}},
            f"JSON schema breaks the JSON Schema rules at $id: $id '{_APPLICATOR}' names a JSON Schema meta-schema: "
            "a schema cannot take a meta-schema's identifier — give it an identifier of its own, or drop the $id",
        ),
    ],
    ids=[
        "pointer-to-nowhere",
        "not-a-schema",
        "cycle",
        "partial-cycle",
        "meta-schema-re-entry",
        "meta-anchor-ref",
        "meta-anchor-dynamic-ref",
        "meta-anchor-without-references",
        "negative-array-index",
        "meta-schema-identifier",
    ],
)
@pytest.mark.parametrize("instance", [{}, {"a": 1}, 1], ids=["reaches-no-ref", "reaches-the-ref", "scalar"])
def test_validator_refuses_every_reference_it_cannot_follow_before_any_instance(schema, refusal, instance):
    with pytest.raises(InvalidJsonSchemaError, match=re.escape(refusal)):
        validate_against_json_schema(instance, schema)
    with pytest.raises(InvalidJsonSchemaError, match=re.escape(refusal)):
        check_json_schema(schema)


def test_validator_admits_a_pointer_into_a_meta_schema_subschema_binding_no_dynamic_name():
    # The meta-schema subschema ``anchorString`` refers to nothing, so the anchor below the root stays unreached.
    schema = {"$id": "https://e.com/r", "properties": {"p": {"$ref": f"{_CORE}#/$defs/anchorString"}}, "$defs": _X_TO_Y}
    check_json_schema(schema)
    validate_against_json_schema({"p": "ab"}, schema)
    with pytest.raises(JsonSchemaValidationError):
        validate_against_json_schema({"p": 5}, schema)


_LABELLED = {
    "type": "object",
    "properties": {"label": {"type": "string", "minLength": 1}},
    "required": ["label"],
    "additionalProperties": False,
}

_SCHEMA_VALUED = {
    "type": "object",
    "properties": {"schema": {"$ref": "https://json-schema.org/draft/2020-12/schema"}},
    "required": ["schema"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("schema", "instance", "message"),
    [
        (_LABELLED, {}, "value does not match schema at $: breaks the schema's 'required' rule: 'label'"),
        (
            _LABELLED,
            {"label": "ok", "x": "s3cr3t-unknown"},
            "value does not match schema at $: breaks the schema's 'additionalProperties' rule: false",
        ),
        (_LABELLED, {"label": 7}, "value does not match schema at $.label: breaks the schema's 'type' rule: 'string'"),
        (_LABELLED, {"label": ""}, "value does not match schema at $.label: breaks the schema's 'minLength' rule: 1"),
        (
            {"type": "object", "properties": {"s": {"anyOf": [{"type": "object"}, {"type": "boolean"}]}}},
            {"s": "s3cr3t-any"},
            "value does not match schema at $.s: breaks the schema's 'anyOf' rule",
        ),
        (
            {"type": "object", "properties": {"s": {"type": ["object", "boolean"]}}},
            {"s": "s3cr3t-scalar"},
            "value does not match schema at $.s: breaks the schema's 'type' rule: 'object', 'boolean'",
        ),
        (
            _SCHEMA_VALUED,
            {"schema": {"type": "s3cr3t-type"}},
            "value does not match schema at $.schema.type: breaks the schema's 'anyOf' rule",
        ),
        (
            _SCHEMA_VALUED,
            {"schema": "s3cr3t-scalar"},
            "value does not match schema at $.schema: breaks the schema's 'type' rule: 'object', 'boolean'",
        ),
        (
            {"type": "object", "properties": {"a": False}},
            {"a": "s3cr3t-closed"},
            "value does not match schema at $: refused by the schema",
        ),
        (
            {"type": "object", "properties": {"n": {"maximum": 2.5}}},
            {"n": 99},
            "value does not match schema at $.n: breaks the schema's 'maximum' rule: 2.5",
        ),
        (
            {"type": "object", "properties": {"n": {"const": None}}},
            {"n": "s3cr3t-null"},
            "value does not match schema at $.n: breaks the schema's 'const' rule: null",
        ),
    ],
)
def test_redacted_mismatch_names_the_path_and_the_rule_never_the_value(
    schema: dict[str, object], instance: object, message: str
) -> None:
    with pytest.raises(JsonSchemaValidationError) as caught:
        validate_against_json_schema(instance, schema, redact_value=True)
    assert str(caught.value) == f"{message} (the value is not shown)"
    assert caught.value.offending_value is None
    assert caught.value.json_path
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "s3cr3t" not in str(caught.value)


def test_redacted_mode_still_refuses_a_broken_schema_first() -> None:
    with pytest.raises(InvalidJsonSchemaError):
        validate_against_json_schema({"a": 1}, {"type": "s3cr3t-type"}, redact_value=True)


def test_redacted_mode_passes_a_conforming_instance() -> None:
    assert validate_against_json_schema({"label": "ok"}, _LABELLED, redact_value=True) is None
