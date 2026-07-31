"""Tests for the ``chain`` tool extension."""

import asyncio
import inspect
import time
from typing import Any

import pytest
import tai42_kit.utils.data.jq_util as jq_util
from tai42_contract.extensions import ExtensionKind
from tai42_kit.settings import reset_all_settings

import tai42_toolbox.extensions.chain as chain_module
from tai42_toolbox._internal.extensions.chain_executor import execute_chain
from tai42_toolbox.extensions.chain import chain

from .conftest import FakeTools


def _tool(user_id: int) -> dict:
    return {"user_id": user_id}


def test_registers_as_transformer_named_chain(capture_registration):
    assert capture_registration(chain_module) == [("chain", ExtensionKind.TRANSFORMER, False)]


def test_wraps_a_tool_ending_in_var_keyword():
    # The two control kwargs must be inserted before a trailing **kwargs so
    # signature construction stays valid for such tools.
    def tool(user_id: int, **kwargs: object) -> dict:
        return {"user_id": user_id}

    composed = inspect.signature(chain(tool, "tool", "desc"))
    names = list(composed.parameters)

    assert names[-1] == "kwargs"
    assert composed.parameters["jq_expression"].kind is inspect.Parameter.KEYWORD_ONLY
    assert composed.parameters["next_tool_name"].kind is inspect.Parameter.KEYWORD_ONLY


def test_composed_signature_is_concrete():
    params = list(inspect.signature(chain(_tool, "tool", "desc")).parameters.values())
    var_kinds = {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}

    assert not all(p.kind in var_kinds for p in params)
    # The wrapped tool's params plus the two chain controls.
    assert {p.name for p in params} == {"user_id", "jq_expression", "next_tool_name"}


def test_chains_first_tool_through_jq_into_second(bind_fake_app):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        calls.append((key, arguments))
        if key == "get_user":
            return {"name": "Alice", "age": 30}
        if key == "greet":
            return {"greeting": f"Hello {arguments['name']}"}
        raise AssertionError(f"unexpected tool {key}")

    bind_fake_app(FakeTools(run_tool=run_tool))

    result = asyncio.run(
        execute_chain(
            tool_name="get_user",
            tool_arguments={"user_id": 1},
            jq_expression="{name: .name}",
            next_tool_name="greet",
        )
    )

    assert result == {"greeting": "Hello Alice"}
    assert calls == [("get_user", {"user_id": 1}), ("greet", {"name": "Alice"})]


def test_jq_expression_evaluates_off_loop_and_value_round_trips(bind_fake_app):
    # The jq expression is evaluated through the off-loop helper; a non-trivial transform must
    # compute the same result and reach the second tool intact.
    received: dict[str, Any] = {}

    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        if key == "measure":
            return {"readings": [3, 1, 2]}
        if key == "sink":
            received.update(arguments)
            return arguments
        raise AssertionError(f"unexpected tool {key}")

    bind_fake_app(FakeTools(run_tool=run_tool))

    result = asyncio.run(
        execute_chain(
            tool_name="measure",
            tool_arguments={},
            jq_expression="{total: (.readings | add), count: (.readings | length)}",
            next_tool_name="sink",
        )
    )

    assert result == {"total": 6, "count": 3}
    assert received == {"total": 6, "count": 3}


class _BlockingProgram:
    """A stand-in compiled jq program whose evaluation blocks on the worker
    thread, standing in for a pathological expression such as ``[range(1e9)]``."""

    def input(self, payload: Any) -> "_BlockingProgram":
        return self

    def first(self) -> Any:
        time.sleep(2)
        return None


def test_pathological_jq_expression_surfaces_timeout_through_chain(bind_fake_app, monkeypatch):
    # A pathological jq expression must not block the loop: the JQ_TIMEOUT_SECONDS budget surfaces
    # as a loud TimeoutError through execute_chain, and the second tool is never called.
    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        if key == "source":
            return {"n": 1}
        raise AssertionError(f"second tool must not run; got {key}")

    bind_fake_app(FakeTools(run_tool=run_tool))

    monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expression: _BlockingProgram())
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.05")
    reset_all_settings()
    try:
        with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
            asyncio.run(
                execute_chain(
                    tool_name="source",
                    tool_arguments={},
                    jq_expression="[range(1e9)]",
                    next_tool_name="sink",
                )
            )
    finally:
        reset_all_settings()
