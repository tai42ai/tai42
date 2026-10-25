"""``schema_signature`` presents an authored object schema as named keyword-only arguments.

A schema it accepts round-trips: the input schema derived from the signature's function is
the authored one, and a schema-preserving wrapper over it passes the wrapper schema check.
A schema it cannot present raises :class:`SchemaSignatureError` naming the reason.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

import pytest
from fastmcp.tools.function_tool import FunctionTool
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.unset import UNSET

from tai42_skeleton.tools.binding.errors import SchemaSignatureError
from tai42_skeleton.tools.binding.extension_schema import _enforce_extension_schema
from tai42_skeleton.tools.binding.schema import _derive_input_schema, schema_signature

_PROPERTIES = {
    "where": {"type": "string", "minLength": 2, "description": "the place"},
    "n": {"type": "integer", "default": 3},
    "tags": {"type": "array", "items": {"type": "string"}},
}
_SCHEMA = {
    "type": "object",
    "title": "Query",
    "description": "a query",
    "properties": _PROPERTIES,
    "required": ["where"],
    "additionalProperties": False,
}


def _function_over(signature: inspect.Signature) -> Any:
    async def presented(**kwargs: Any) -> Any:
        return kwargs

    presented.__signature__ = signature  # type: ignore[attr-defined]
    presented.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()}
    return presented


def _advertised(signature: inspect.Signature) -> dict[str, Any]:
    return FunctionTool.from_function(_function_over(signature), name="presented").parameters


def test_the_signature_has_one_keyword_only_parameter_per_property_in_order():
    signature = schema_signature(_SCHEMA)

    params = list(signature.parameters.values())
    assert [p.name for p in params] == ["where", "n", "tags"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)
    assert params[0].default is inspect.Parameter.empty
    assert params[1].default is UNSET
    assert params[2].default is UNSET
    assert signature.return_annotation is inspect.Signature.empty


def test_a_tool_over_the_signature_advertises_the_authored_input_schema():
    assert _advertised(schema_signature(_SCHEMA)) == {
        "type": "object",
        "properties": _PROPERTIES,
        "required": ["where"],
        "additionalProperties": False,
    }


def test_a_schema_with_no_required_names_advertises_no_required_list():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "additionalProperties": False}

    assert _advertised(schema_signature(schema)) == schema


def test_the_derived_input_schema_carries_each_property_subschema():
    derived = _derive_input_schema(_function_over(schema_signature(_SCHEMA)))

    assert derived["required"] == ["where"]
    assert derived["additionalProperties"] is False
    for name, subschema in _PROPERTIES.items():
        assert {k: v for k, v in derived["properties"][name].items() if k != "title"} == subschema


def test_a_wrapper_over_the_signature_passes_the_wrapper_schema_check():
    base = _function_over(schema_signature(_SCHEMA))

    @functools.wraps(base)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await base(*args, **kwargs)

    def factory(func: Any, name: str, desc: str, config: Any = None) -> Any:
        return wrapper

    _enforce_extension_schema("w", ExtensionKind.WRAPPER, factory, base, wrapper, "p")


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "array", "additionalProperties": False}, 'the schema root must be "type": "object"'),
        ({"properties": {}, "additionalProperties": False}, 'the schema root must be "type": "object"'),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}},
            'the schema must set "additionalProperties": false',
        ),
        (
            {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}},
            'the schema must set "additionalProperties": false',
        ),
        (
            {"type": "object", "anyOf": [{"required": ["a"]}], "additionalProperties": False},
            "the schema root keyword 'anyOf' cannot be presented as named arguments",
        ),
        (
            {"type": "object", "$defs": {"x": {}}, "additionalProperties": False},
            "the schema root keyword '$defs' cannot be presented as named arguments",
        ),
        (
            {"type": "object", "properties": {"a": True}, "additionalProperties": False},
            "property 'a' must hold an object schema",
        ),
        (
            {"type": "object", "properties": {"a": {"$ref": "#/$defs/x"}}, "additionalProperties": False},
            "property 'a' holds a $ref",
        ),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "array", "items": {"$ref": "#/$defs/x"}}},
                "additionalProperties": False,
            },
            "property 'a' holds a $ref",
        ),
        (
            {"type": "object", "properties": {"first-name": {"type": "string"}}, "additionalProperties": False},
            "property name 'first-name' is not a valid parameter name",
        ),
        (
            {"type": "object", "properties": {"class": {"type": "string"}}, "additionalProperties": False},
            "property name 'class' is not a valid parameter name",
        ),
        (
            {"type": "object", "properties": {}, "required": ["a"], "additionalProperties": False},
            "required name 'a' is not a property",
        ),
    ],
)
def test_a_schema_that_cannot_be_presented_is_refused(schema: dict[str, Any], message: str):
    with pytest.raises(SchemaSignatureError) as caught:
        schema_signature(schema)

    assert str(caught.value) == message
