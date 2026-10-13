"""The framework-neutral client-tool handle, driven by a neutral in-process engine.

``_Engine`` is not LangChain and copies no real engine: it holds the handles ``get_client_tools``
returns and calls ``invoke`` by name, turning a :class:`ClientToolError` into the text a model would
read and letting every other error leave the call. Through it the handle proves its contract — the
advertised schema is the MCP edge's, validation is ``run_tool``'s, and the platform work (tier fence,
identity gate, masking, the undeclared-park refusal) happens inside ``invoke``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    ResumeBuffered,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.tools import ClientTool, ClientToolError, UndeclaredPauseError
from tai42_kit.llm import ToolNameTooLongError
from tests.app._fixtures.pause_counter import BODY_RUNS as PAUSE_BODY_RUNS

import tai42_skeleton.operations._authority as authority
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.errors import ForbiddenError
from tai42_skeleton.tools.binding.errors import UnknownToolError

from ._client_handle_counter import BODY_RUNS, OVERLONG_NAME

tai42_app.bind(app)

_TOOLS = "tests.tools._client_handle_fixtures"
_PAUSE_TOOLS = "tests.app._fixtures.pause_tools"
_HANDLED = ["five_fields", "with_context", "with_dependency", "holds_a_secret", "body_fails", "terminal_fails"]


class _Engine:
    """A neutral in-process engine over client-tool handles."""

    def __init__(self, handles: list[ClientTool]) -> None:
        self.handles = {handle.name: handle for handle in handles}

    def schema(self, name: str) -> dict[str, Any]:
        return self.handles[name].input_schema

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return await self.handles[name].invoke(arguments)

    async def call_for_model(self, name: str, arguments: Mapping[str, Any]) -> str:
        try:
            return json.dumps(await self.call(name, arguments))
        except ClientToolError as exc:
            return f"Error: {exc}"


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    BODY_RUNS.clear()
    PAUSE_BODY_RUNS.clear()
    yield
    asyncio.run(_clear())


def _manifest(*, overlong: bool = False) -> Manifest:
    include = [*_HANDLED, "unencodable", *([OVERLONG_NAME] if overlong else [])]
    return Manifest.model_validate(
        {
            "tools": [
                {"title": "handles", "module": _TOOLS, "include": include},
                {
                    "title": "pauses",
                    "module": _PAUSE_TOOLS,
                    "include": [
                        "undeclared_park",
                        "undeclared_buffered",
                        "declared_park",
                        "declared_buffered",
                        "redispatcher",
                    ],
                },
                {"title": "presets", "module": "tests.presets._fixtures", "include": ["weather"]},
            ],
            "agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["echo_fields"]}],
        }
    )


async def _engine(*names: str) -> _Engine:
    return _Engine(await app.tools.get_client_tools(list(names)))


# -- the handle's shape -------------------------------------------------------------------


def test_every_handle_is_a_client_tool_with_the_full_registered_name():
    async def run() -> None:
        async with app.app_context(_manifest()):
            handles = await app.tools.get_client_tools(["five_fields", "echo_fields"])
            assert [handle.name for handle in handles] == ["five_fields", "echo_fields"]
            assert all(isinstance(handle, ClientTool) for handle in handles)
            assert handles[0].description == "Echo five typed fields."

    asyncio.run(run())


@pytest.mark.parametrize("name", ["five_fields", "with_context", "with_dependency", "paris", "echo_fields"])
def test_the_schema_is_the_tools_advertised_parameters(name, preset_manager_restored):
    # A plain tool, a ctx tool, a Depends tool, a preset and an agent run tool all advertise
    # exactly what the MCP edge advertises — injected parameters stripped, baked keys hidden.
    async def run() -> None:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("paris", "weather", {"units": "imperial"}, [], "Paris weather")
            engine = await _engine(name)
            assert engine.schema(name) == (await app.tools.get_tool(name)).parameters
            properties = engine.schema(name).get("properties", {})
            assert "ctx" not in properties
            assert "factor" not in properties
            assert "units" not in properties

    asyncio.run(run())


def test_an_unknown_name_raises():
    async def run() -> None:
        async with app.app_context(_manifest()):
            with pytest.raises(UnknownToolError):
                await app.tools.get_client_tools(["no_such_tool"])

    asyncio.run(run())


def test_a_name_past_the_bound_raises_and_is_never_truncated():
    async def run() -> None:
        async with app.app_context(_manifest(overlong=True)):
            with pytest.raises(ToolNameTooLongError, match=OVERLONG_NAME):
                await app.tools.get_client_tools([OVERLONG_NAME])

    asyncio.run(run())


# -- the call -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"a": 1, "b": "x"},
        {"a": "2", "b": "x", "c": "1.5", "d": "true", "e": ["p"]},
        {"a": "not a number", "b": "x"},
        {"b": "x"},
        {"a": 1, "b": "x", "extra": True},
        {"a": 1, "b": ["not", "a", "string"]},
    ],
    ids=["minimal", "coerced", "bad-int", "missing-required", "unknown-key", "bad-str"],
)
def test_validation_accepts_and_refuses_exactly_as_run_tool(arguments):
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine("five_fields")
            try:
                expected = await app.tools.run_tool("five_fields", dict(arguments))
            except ValidationError:
                with pytest.raises(
                    ClientToolError, match="Error calling tool 'five_fields': invalid arguments"
                ) as caught:
                    await engine.call("five_fields", arguments)
                assert isinstance(caught.value.__cause__, ValidationError)
                assert caught.value.tool_name == "five_fields"
            else:
                assert await engine.call("five_fields", arguments) == expected

    asyncio.run(run())


def test_a_refused_argument_set_never_reaches_the_body():
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine("five_fields")
            text = await engine.call_for_model("five_fields", {"b": "x"})
            assert text.startswith("Error: Error calling tool 'five_fields': invalid arguments")

    asyncio.run(run())
    assert BODY_RUNS.get("five_fields") is None


def test_injected_parameters_resolve_at_call_time():
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine("with_context", "with_dependency")
            # The in-process bridge hands the body the platform's own Context.
            assert await engine.call("with_context", {"q": "hi"}) == "hi:PlatformBridgeContext"
            assert await engine.call("with_dependency", {"n": 2}) == 6

    asyncio.run(run())


def test_a_preset_serves_its_baked_constant_and_refuses_a_baked_key(preset_manager_restored):
    async def run() -> None:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("paris", "weather", {"units": "imperial"}, [], "Paris weather")
            engine = await _engine("paris")
            assert await engine.call("paris", {"city": "paris"}) == {"city": "paris", "units": "imperial"}
            with pytest.raises(ClientToolError, match="invalid arguments"):
                await engine.call("paris", {"city": "paris", "units": "metric"})

    asyncio.run(run())


def test_an_agent_run_tool_forwards_only_the_supplied_keys():
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine("echo_fields")
            # ``echo_fields`` echoes the run kwargs it received: the omitted optionals never arrive.
            assert await engine.call("echo_fields", {"text": "hi"}) == "text"

    asyncio.run(run())


def test_secrets_are_masked_inside_invoke():
    async def run() -> None:
        async with app.app_context(_manifest()):
            out = await (await _engine("holds_a_secret")).call("holds_a_secret", {})
            assert out == {"token": "[secret]"}

    asyncio.run(run())


def test_the_tier_fence_runs_inside_invoke(monkeypatch: pytest.MonkeyPatch):
    async def _non_admin() -> Any:
        return type("_Caller", (), {"is_admin": False})()

    monkeypatch.setattr(authority, "resolve_caller", _non_admin)

    async def run() -> None:
        async with app.app_context(_manifest()):

            @app.tools.tool(force=True, tier="fenced")
            async def fenced_probe(q: str) -> str:
                """A fenced body."""
                return q

            # Binding the handle decides nothing; the call is refused raw.
            engine = await _engine("fenced_probe")
            with pytest.raises(ForbiddenError):
                await engine.call_for_model("fenced_probe", {"q": "x"})

    asyncio.run(run())


def test_the_identity_gate_runs_inside_invoke_on_the_supplied_arguments(monkeypatch: pytest.MonkeyPatch):
    decided: list[tuple[str, dict[str, Any]]] = []

    class _RefusedError(PermissionError):
        pass

    async def run() -> None:
        async with app.app_context(_manifest()):
            binding = app._tool_binding
            engine = await _engine("five_fields")

            async def _decide(identity: Any, key: str, arguments: dict[str, Any]) -> None:
                decided.append((key, arguments))
                raise _RefusedError(key)

            monkeypatch.setattr(binding, "_bound_execution_identity", lambda: object())
            monkeypatch.setattr(binding, "_authorize_execution_dispatch", _decide)
            with pytest.raises(_RefusedError):
                await engine.call_for_model("five_fields", {"a": 1, "b": "x"})

    asyncio.run(run())
    assert decided == [("five_fields", {"a": 1, "b": "x"})]
    assert BODY_RUNS.get("five_fields") is None


def test_a_body_failure_a_failed_terminal_and_an_unencodable_result_reach_the_model():
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine("body_fails", "terminal_fails", "unencodable")
            assert await engine.call_for_model("body_fails", {"q": "x"}) == (
                "Error: Error calling tool 'body_fails': boom x"
            )
            failed = await engine.call_for_model("terminal_fails", {})
            assert failed == 'Error: {"status": "failed", "reason": "the step failed"}'
            assert await engine.call_for_model("unencodable", {}) == (
                "Error: tool 'unencodable' produced a result that cannot be JSON-encoded at $.text"
            )

    asyncio.run(run())


def test_a_vanished_registration_fails_raw(monkeypatch: pytest.MonkeyPatch):
    async def run() -> None:
        async with app.app_context(_manifest()):
            binding = app._tool_binding
            engine = await _engine("five_fields")
            app.tools.remove_tool("five_fields")
            monkeypatch.setattr(binding, "_bound_execution_identity", lambda: object())
            with pytest.raises(UnknownToolError):
                await engine.call_for_model("five_fields", {"a": 1, "b": "x"})

    asyncio.run(run())


# -- parks --------------------------------------------------------------------------------


def test_a_declared_park_returns_the_typed_signals_unadapted():
    async def run() -> tuple[Any, Any]:
        async with app.app_context(_manifest()):
            engine = await _engine("declared_park", "declared_buffered")
            token = set_resume_continuation_tool("engine_resume")
            try:
                return await engine.call("declared_park", {"q": "x"}), await engine.call(
                    "declared_buffered", {"q": "x"}
                )
            finally:
                reset_resume_continuation_tool(token)

    parked, buffered = asyncio.run(run())
    assert isinstance(parked, SuspendedInteraction)
    assert parked.interaction_id == "declared"
    assert parked.interaction_ids == ["declared"]
    assert parked.caller_interaction_ids == []
    assert parked.resume_owner == "engine_resume"
    assert isinstance(buffered, ResumeBuffered)
    assert buffered.remaining_ids == ["other"]


@pytest.mark.parametrize("tool", ["undeclared_park", "undeclared_buffered"])
def test_an_undeclared_park_raises_raw(tool):
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine(tool)
            with pytest.raises(UndeclaredPauseError) as caught:
                await engine.call_for_model(tool, {"q": "x"})
            assert not isinstance(caught.value, ClientToolError)
            assert caught.value.tool == tool

    asyncio.run(run())
    assert PAUSE_BODY_RUNS[tool] == 1


def test_a_nested_undeclared_park_leaves_the_handle_raw():
    async def run() -> None:
        async with app.app_context(_manifest()):
            engine = await _engine("redispatcher")
            with pytest.raises(UndeclaredPauseError) as caught:
                await engine.call_for_model("redispatcher", {"target": "undeclared_park"})
            assert caught.value.tool == "undeclared_park"

    asyncio.run(run())
    assert PAUSE_BODY_RUNS["undeclared_park"] == 1
