"""Tests for the ``output_schema`` tool extension."""

import asyncio
import inspect

import pytest
from fastmcp.tools.function_parsing import ParsedFunction
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.data.json_schema_util import (
    InvalidJsonSchemaError,
    JsonSchemaValidationError,
)

import tai42_toolbox.extensions.output_schema as output_schema_module
from tai42_toolbox.extensions.cache import cache
from tai42_toolbox.extensions.output_schema import output_schema

_USER_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 3}},
    "required": ["name"],
    "additionalProperties": False,
}


def _get_user(user_id: int) -> dict:
    return {"name": "Alice"}


def _advertised_output_schema(branch):
    """The output schema the platform would advertise for ``branch`` — derived
    exactly as the skeleton binder does, from the branch's return annotation."""
    return ParsedFunction.from_function(branch).output_schema


def test_registers_as_transformer_named_output_schema(capture_registration):
    assert capture_registration(output_schema_module) == [("output_schema", ExtensionKind.TRANSFORMER, False)]


def test_factory_is_config_accepting_four_arg():
    # The apply site binds author config only to a factory that declares a ``config`` parameter
    # after ``func``/``name``/``description``, so the signature must present it.
    params = inspect.signature(output_schema).parameters
    assert list(params) == ["func", "orig_name", "orig_desc", "config"]
    config_param = params["config"]
    assert config_param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def test_branch_is_named_and_concrete():
    branch = output_schema(_get_user, "get_user", "desc", {"schema": _USER_SCHEMA})
    assert branch.__name__ == "get_user_output_schema"
    # The branch presents the wrapped tool's own concrete input signature.
    assert set(inspect.signature(branch).parameters) == {"user_id"}


def test_branch_advertises_the_configured_schema():
    branch = output_schema(_get_user, "get_user", "desc", {"schema": _USER_SCHEMA})
    advertised = _advertised_output_schema(branch)

    assert advertised is not None
    assert advertised["type"] == "object"
    assert advertised["required"] == ["name"]
    assert advertised["additionalProperties"] is False
    # The constraint keyword survives onto the advertised schema (fidelity).
    assert advertised["properties"]["name"] == {"type": "string", "minLength": 3}


def test_conforming_result_passes_through_unchanged():
    branch = output_schema(_get_user, "get_user", "desc", {"schema": _USER_SCHEMA})
    assert asyncio.run(branch(user_id=1)) == {"name": "Alice"}


def test_mismatch_raises_with_json_path():
    def missing_name(user_id: int) -> dict:
        return {"other": "value"}

    branch = output_schema(missing_name, "u", "desc", {"schema": _USER_SCHEMA})
    with pytest.raises(JsonSchemaValidationError) as excinfo:
        asyncio.run(branch(user_id=1))
    assert excinfo.value.json_path == "$"


def test_constraint_keyword_is_enforced_at_runtime():
    # A value satisfying the type but violating a constraint keyword (``minLength``) is rejected at
    # runtime, with the JSON-path and offending value carried on the error.
    def too_short(user_id: int) -> dict:
        return {"name": "Al"}

    branch = output_schema(too_short, "u", "desc", {"schema": _USER_SCHEMA})
    with pytest.raises(JsonSchemaValidationError) as excinfo:
        asyncio.run(branch(user_id=1))
    assert excinfo.value.json_path == "$.name"
    assert excinfo.value.offending_value == "Al"


def test_pattern_constraint_is_enforced_at_runtime():
    schema = {
        "type": "object",
        "properties": {"code": {"type": "string", "pattern": "^[A-Z]{3}$"}},
        "required": ["code"],
    }

    def wrong_code(x: int) -> dict:
        return {"code": "abc"}

    branch = output_schema(wrong_code, "t", "desc", {"schema": schema})
    with pytest.raises(JsonSchemaValidationError) as excinfo:
        asyncio.run(branch(x=1))
    assert excinfo.value.json_path == "$.code"


def test_missing_schema_key_fails_the_bind_loudly():
    with pytest.raises(ValueError, match="requires a 'schema' key"):
        output_schema(_get_user, "get_user", "desc", {})


def test_meta_schema_invalid_schema_fails_the_bind_loudly():
    # A present-but-malformed schema is a distinct failure from a missing key: it
    # raises the kit validator's meta-schema error, not the missing-key ValueError.
    with pytest.raises(InvalidJsonSchemaError):
        output_schema(_get_user, "get_user", "desc", {"schema": {"type": "not-a-real-type"}})


def test_composes_over_another_extensions_branch():
    # output_schema composes over a branch from another extension (a cache WRAPPER here): it wraps
    # the branch, validates its result, and re-advertises the configured schema.
    calls: list[int] = []

    def base(user_id: int) -> dict:
        calls.append(user_id)
        return {"name": "Alice"}

    cached = cache(base, "base", "desc")
    composed = output_schema(cached, "base_cache", "desc", {"schema": _USER_SCHEMA})

    assert asyncio.run(composed(user_id=7)) == {"name": "Alice"}
    assert calls == [7]
    advertised = _advertised_output_schema(composed)
    assert advertised is not None
    assert advertised["required"] == ["name"]
