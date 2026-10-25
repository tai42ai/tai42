"""The omitted-value marker: an optional parameter's default a caller may leave out, and its strip."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic.json_schema import PydanticJsonSchemaWarning

from tai42_kit.utils.unset import UNSET, without_unset


def test_without_unset_drops_only_the_marker_into_a_new_dict() -> None:
    supplied = {"a": 1, "b": UNSET, "c": None}
    kept = without_unset(supplied)
    assert kept == {"a": 1, "c": None}
    assert kept is not supplied
    assert supplied == {"a": 1, "b": UNSET, "c": None}


def test_a_parameter_defaulting_to_the_marker_is_optional_with_no_default_in_its_schema() -> None:
    def f(a: int, b: Any = UNSET) -> None: ...

    with pytest.warns(PydanticJsonSchemaWarning, match="is not JSON serializable; excluding default"):
        schema = TypeAdapter(f).json_schema()
    assert schema["required"] == ["a"]
    assert "b" in schema["properties"]
    assert "default" not in schema["properties"]["b"]
