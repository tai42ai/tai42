"""Tests for the ``output_schema`` extension's runtime validator."""

import asyncio

import pytest
from pydantic import BaseModel
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_toolbox._internal.extensions.output_schema_validator import enforce_output_schema

_SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}


def test_sync_func_conforming_result_returned_unchanged():
    def tool(n: int) -> dict:
        return {"n": n}

    result = asyncio.run(enforce_output_schema(tool, (), {"n": 4}, _SCHEMA))
    assert result == {"n": 4}


def test_async_func_conforming_result_returned_unchanged():
    async def tool(n: int) -> dict:
        return {"n": n}

    result = asyncio.run(enforce_output_schema(tool, (), {"n": 4}, _SCHEMA))
    assert result == {"n": 4}


def test_pydantic_model_result_is_validated_in_its_jsonable_form():
    # A tool that returns a rich value (a pydantic model) is validated against the
    # schema in the JSON-able form the platform serializes — and returned as-is.
    class Out(BaseModel):
        n: int

    def tool() -> Out:
        return Out(n=9)

    result = asyncio.run(enforce_output_schema(tool, (), {}, _SCHEMA))
    assert isinstance(result, Out)
    assert result.n == 9


def test_mismatch_raises_loudly():
    def tool() -> dict:
        return {"n": "not-an-int"}

    with pytest.raises(JsonSchemaValidationError) as excinfo:
        asyncio.run(enforce_output_schema(tool, (), {}, _SCHEMA))
    assert excinfo.value.json_path == "$.n"
