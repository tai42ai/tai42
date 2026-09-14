"""The pure schema validators and the page-size clamp — no store, no service instance."""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import SchemaValidationError, ValueValidationError

from tai42_skeleton.states.schema import _validate_document, _validate_schema
from tai42_skeleton.states.service.rows import _page_limit


def test_validate_schema_shape_refusals() -> None:
    with pytest.raises(SchemaValidationError, match="must be a JSON object"):
        _validate_schema("nope")
    with pytest.raises(SchemaValidationError, match='"type": "object"'):
        _validate_schema({"type": "string"})
    with pytest.raises(SchemaValidationError, match="at least one property"):
        _validate_schema({"type": "object", "properties": {}})
    with pytest.raises(SchemaValidationError, match="not a valid JSON Schema"):
        _validate_schema({"type": "object", "properties": {"n": {"type": 123}}})


def test_validate_schema_accepts_resolvable_local_refs() -> None:
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/x"}, "b": {"$ref": "#"}, "c": {"$ref": "#/allOf/0"}},
            "allOf": [{"title": "t"}],
            "$defs": {"x": {"type": "integer"}},
        }
    )
    # a #anchor that resolves (nested inside a list, exercising the list-walk)
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#named"}},
            "allOf": [{"$anchor": "named", "type": "object"}],
        }
    )


def test_validate_schema_percent_decodes_refs() -> None:
    # RFC 6901 §6: a URI-fragment pointer is percent-decoded WHOLE, then split on "/", then
    # ~1/~0-unescaped — matching jsonschema's resolver, so this syntax pre-check accepts
    # exactly what _validate_document later resolves. "%20" decodes to a space in the key.
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/a%20b"}},
            "$defs": {"a b": {"type": "integer"}},
        }
    )
    # A key that literally contains "/" is named with ~1 (RFC 6901), never %2F: %2F decodes
    # to a separator before the split, so only #/$defs/a~1b reaches the key "a/b".
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/a~1b"}},
            "$defs": {"a/b": {"type": "integer"}},
        }
    )
    # %2F decodes to a separator before the split, so it can never name a key holding "/".
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/a%2Fb"}},
                "$defs": {"a/b": {"type": "integer"}},
            }
        )
    # A percent-encoded ref that names no key is still refused loudly.
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/x%20y"}},
                "$defs": {"a b": {"type": "integer"}},
            }
        )


def test_validate_schema_ref_refusals() -> None:
    with pytest.raises(SchemaValidationError, match="remote"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}})
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})
    with pytest.raises(SchemaValidationError, match="no \\$anchor"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#absent"}}})
    with pytest.raises(SchemaValidationError, match="dynamicRef"):
        _validate_schema({"type": "object", "properties": {"x": {"type": "string"}}, "allOf": [{"$dynamicRef": "#m"}]})


def test_validate_document_reports_unresolvable_ref() -> None:
    # A schema whose $ref cannot be resolved at validation time surfaces as a loud value
    # error, never an opaque referencing exception.
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    with pytest.raises(ValueValidationError):
        _validate_document(schema, {"a": 1})


def test_page_limit_clamps_and_refuses() -> None:
    assert _page_limit(None) == 200
    assert _page_limit(10) == 10
    assert _page_limit(10_000) == 500  # clamped to the hard cap
    with pytest.raises(ValueValidationError, match="positive integer"):
        _page_limit(0)
    with pytest.raises(ValueValidationError, match="positive integer"):
        _page_limit(True)
