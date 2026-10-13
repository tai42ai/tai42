"""The framework-neutral in-process tool handle and the tool-surface errors the contract declares."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, ClassVar

import pytest

from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.tools import ClientTool, ClientToolError, ToolAlreadyRegisteredError


class _MinimalHandle:
    """The smallest object an in-process engine needs to drive a bound tool."""

    name = "echo"
    description = "Echo its arguments."
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def invoke(self, arguments: Mapping[str, Any]) -> Any:
        return dict(arguments)


def test_a_minimal_class_satisfies_the_client_tool_protocol():
    handle = _MinimalHandle()
    assert isinstance(handle, ClientTool)
    assert asyncio.run(handle.invoke({"x": 1})) == {"x": 1}


def test_an_object_without_invoke_is_not_a_client_tool():
    class _NoInvoke:
        name = "n"
        description = ""
        input_schema: ClassVar[dict[str, Any]] = {}

    assert not isinstance(_NoInvoke(), ClientTool)


def test_client_tool_error_carries_the_tool_name_and_the_model_facing_message():
    error = ClientToolError("echo", "Error calling tool 'echo': boom")
    assert error.tool_name == "echo"
    assert str(error) == "Error calling tool 'echo': boom"


def test_client_tool_error_takes_its_kind_from_its_cause():
    def fail() -> None:
        try:
            raise TimeoutError("slow")
        except TimeoutError as exc:
            raise ClientToolError("echo", "slow") from exc

    with pytest.raises(ClientToolError) as caught:
        fail()
    assert error_kind(caught.value) is ErrorKind.TIMED_OUT


def test_tool_already_registered_is_a_conflict_value_error_naming_the_tool():
    error = ToolAlreadyRegisteredError("x")
    assert isinstance(error, ValueError)
    assert error.name == "x"
    assert str(error) == "tool 'x' is already registered"
    assert error_kind(error) is ErrorKind.CONFLICT
