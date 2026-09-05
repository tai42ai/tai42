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


def test_composed_variants_listed_schema_annotates_the_jq_param():
    # The LISTED tool schema — the platform derives it from the composed callable
    # exactly as here (FastMCP ``Tool.from_function``) — must carry the
    # ``x-tai42-expression`` vendor annotation on ``jq_expression``: makefun
    # composition must not strip the ``Annotated`` parameter metadata. The
    # payload is pinned VERBATIM (it is the wire contract schema consumers read),
    # and the annotation must stay confined to the one jq-typed parameter.
    from fastmcp.tools import Tool

    properties = Tool.from_function(chain(_tool, "tool", "desc")).parameters["properties"]

    assert properties["jq_expression"]["type"] == "string"
    assert properties["jq_expression"]["x-tai42-expression"] == {
        "language": "jq",
        "label": "expression",
        "blurb": "the first tool's raw output",
        "returns": "the second tool's kwargs object",
    }
    assert "x-tai42-expression" not in properties["next_tool_name"]
    assert "x-tai42-expression" not in properties["user_id"]


def test_composed_variants_listed_schema_is_otherwise_unchanged():
    # Byte-identity guard: apart from the one added vendor key, the composed
    # variant's listed schema must equal the schema of the same composition with
    # a PLAIN ``str`` jq param — the annotation is additive-only.
    import inspect as _inspect
    import json

    from fastmcp.tools import Tool
    from makefun import create_function

    from tai42_toolbox._internal.extensions.signature import with_added_params

    branch = chain(_tool, "tool", "desc")
    annotated = Tool.from_function(branch).parameters

    plain_sig = with_added_params(
        _inspect.signature(_tool).replace(return_annotation=Any),
        _inspect.Parameter("jq_expression", _inspect.Parameter.KEYWORD_ONLY, annotation=str),
        _inspect.Parameter("next_tool_name", _inspect.Parameter.KEYWORD_ONLY, annotation=str),
    )

    async def impl(*args: Any, **kwargs: Any):
        return None

    # Same composed docstring so FastMCP parses identical per-param descriptions
    # for both — the diff under test is the annotation alone.
    plain = Tool.from_function(
        create_function(func_signature=plain_sig, func_impl=impl, func_name="tool_chain", doc=branch.__doc__)
    ).parameters

    assert annotated["properties"]["jq_expression"].pop("x-tai42-expression")
    assert json.dumps(annotated, sort_keys=True) == json.dumps(plain, sort_keys=True)


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


def test_a_parked_first_stage_propagates_without_touching_jq_or_the_next_tool(bind_fake_app):
    # The first stage async-parks: its run_tool returns the SuspendedInteraction SIGNAL. The
    # chain must PROPAGATE it verbatim — never feed the sentinel into jq (which would fail to
    # read it as data) nor hand it to the second tool. The chain parks as a whole.
    from datetime import UTC, datetime

    from tai42_contract.interactions import SuspendedInteraction

    calls: list[str] = []
    # The park's resume owner (stamped when the ask parked) rides ON the sentinel, so a downstream
    # claimer still checks it owns it. The chain must re-surface this exact object, not re-mint one.
    parked = SuspendedInteraction(
        interaction_id="i-chain", expiry_at=datetime(2026, 6, 1, tzinfo=UTC), resume_owner="nested_driver_resume"
    )

    async def run_tool(key: str, arguments: dict[str, Any]) -> Any:
        calls.append(key)
        if key == "source":
            return parked
        raise AssertionError(f"the next tool must not run on a parked first stage; got {key}")

    bind_fake_app(FakeTools(run_tool=run_tool))

    result = asyncio.run(
        execute_chain(
            tool_name="source",
            tool_arguments={},
            # A jq expression that WOULD fault on a non-object input, proving jq was skipped.
            jq_expression=".name",
            next_tool_name="sink",
        )
    )

    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i-chain"
    # Propagated WHOLE: the SAME sentinel object survives (byte-identical), so its resume owner
    # rides through and the park stays claimable downstream.
    assert result is parked
    assert result.resume_owner == "nested_driver_resume"
    # Only the first stage ran; jq and the second tool were skipped.
    assert calls == ["source"]


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

    monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expression, prelude="": _BlockingProgram())
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
