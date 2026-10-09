"""Only a tool that declares ``TOOL_META_PAUSES`` may return a park signal — through a test-only consumer.

The refusal holds at the three park-recognition seams every tool dispatch reaches: the shared
``run_tool`` seam, the MCP ``tools/call`` edge, and the in-process agent client-tool adapter.
``app.tools.pauses`` answers for a tool, its presets and its extension branches.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from langchain_core.tools import ToolException
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    ResumeBuffered,
    SuspendedInteraction,
    read_suspended_interaction_marker,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)
from tai42_contract.tools import UndeclaredPauseError
from tests.app._fixtures.pause_counter import BODY_RUNS

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.dispatch_scope import DispatchScopeMiddleware

tai42_app.bind(app)

_TOOLS = "tests.app._fixtures.pause_tools"
_EXTENSIONS = "tests.app._fixtures.pause_extensions"
_REFUSAL = (
    "tool {!r} returned a park signal but does not declare that it can pause; "
    "register it with meta={{'tai42/pauses': True}}"
)


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    BODY_RUNS.clear()
    yield
    asyncio.run(_clear())


def _manifest(extensions: dict[str, list[list[str]]] | None = None) -> Manifest:
    return Manifest.model_validate(
        {
            "extensions_modules": [_EXTENSIONS],
            "tools": [{"title": "pauses", "module": _TOOLS, "extensions": extensions or {}}],
        }
    )


class _SpyRuns:
    def __init__(self) -> None:
        self.outcomes: list[str] = []

    async def insert_start(self, run_id, preset_name, preset_version, **_kwargs: Any) -> None:
        return None

    async def update_outcome(self, run_id, outcome, ended_at, **_kwargs: Any) -> None:
        self.outcomes.append(outcome)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> _SpyRuns:
    import tai42_skeleton.runs.chokepoint as chokepoint

    spy = _SpyRuns()
    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: spy)
    monkeypatch.setattr(chokepoint, "_safe_trace_id", lambda: None)
    return spy


# --- seam 1: run_tool ------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["undeclared_park", "undeclared_buffered"])
def test_run_tool_refuses_an_undeclared_park_naming_the_tool(tool, runs):
    async def run() -> None:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("over_" + tool, tool, {}, [], "d")
            with pytest.raises(UndeclaredPauseError) as caught:
                await app.tools.run_tool("over_" + tool, {"q": "x"})
            assert caught.value.tool == "over_" + tool
            assert str(caught.value) == _REFUSAL.format("over_" + tool)

    asyncio.run(run())
    assert runs.outcomes == ["error"]


@pytest.mark.parametrize(
    ("tool", "kind"), [("declared_park", SuspendedInteraction), ("declared_buffered", ResumeBuffered)]
)
def test_run_tool_returns_a_declared_park(tool, kind):
    async def run() -> Any:
        async with app.app_context(_manifest()):
            return await app.tools.run_tool(tool, {"q": "x"})

    assert isinstance(asyncio.run(run()), kind)


# --- seam 2: the MCP tools/call edge --------------------------------------------------------


def _marker_result(interaction_id: str) -> ToolResult:
    return ToolResult(structured_content=suspended_interaction_marker(interaction_id, None, None))


def test_the_mcp_edge_refuses_an_undeclared_park_as_a_tool_error(runs):
    async def run() -> None:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("mcp_undeclared", "undeclared_park", {}, [], "d")
            context = SimpleNamespace(
                message=SimpleNamespace(name="mcp_undeclared", arguments={"q": "x"}), fastmcp_context=None
            )

            async def call_next(_ctx: object) -> ToolResult:
                return _marker_result("undeclared")

            with pytest.raises(ToolError) as caught:
                await DispatchScopeMiddleware(app).on_call_tool(cast(MiddlewareContext[Any], context), call_next)
            assert str(caught.value) == _REFUSAL.format("mcp_undeclared")
            assert isinstance(caught.value.__cause__, UndeclaredPauseError)

    asyncio.run(run())
    assert runs.outcomes == ["error"]


def test_the_mcp_edge_records_a_declared_park(runs):
    async def run() -> ToolResult:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("mcp_declared", "declared_park", {}, [], "d")
            context = SimpleNamespace(
                message=SimpleNamespace(name="mcp_declared", arguments={"q": "x"}), fastmcp_context=None
            )

            async def call_next(_ctx: object) -> ToolResult:
                return _marker_result("declared")

            return await DispatchScopeMiddleware(app).on_call_tool(cast(MiddlewareContext[Any], context), call_next)

    result = asyncio.run(run())
    assert read_suspended_interaction_marker(result.structured_content) is not None
    assert runs.outcomes == ["parked"]


def test_the_mcp_tools_call_answers_an_error_result_naming_the_tool():
    from fastmcp import Client

    async def run() -> Any:
        async with app.app_context(_manifest()), Client(app._fast_mcp) as client:
            return await client.call_tool("undeclared_park", {"q": "x"}, raise_on_error=False)

    result = asyncio.run(run())
    assert result.is_error is True
    assert result.content[0].text == _REFUSAL.format("undeclared_park")


# --- seam 3: the in-process agent client-tool adapter ---------------------------------------


async def _client_handle(name: str):
    (handle,) = await app.tools.get_client_tools([name])
    return handle


@pytest.mark.parametrize("tool", ["undeclared_park", "undeclared_buffered"])
def test_the_client_tool_adapter_refuses_an_undeclared_park_raw(tool, monkeypatch):
    import tai42_skeleton.tools.binding.client_tools as client_tools

    def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("park adoption must not be reached")

    monkeypatch.setattr(client_tools, "resolve_park_adoption", _never)

    async def run() -> None:
        async with app.app_context(_manifest()):
            handle = await _client_handle(tool)
            with pytest.raises(UndeclaredPauseError) as caught:
                await handle.ainvoke({"q": "x"})
            assert not isinstance(caught.value, ToolException)
            assert caught.value.tool == tool

    asyncio.run(run())


def test_a_nested_refusal_leaves_the_handle_raw_and_the_tool_ran_once():
    async def run() -> None:
        async with app.app_context(_manifest()):
            handle = await _client_handle("redispatcher")
            with pytest.raises(UndeclaredPauseError) as caught:
                await handle.ainvoke({"target": "undeclared_park"})
            assert caught.value.tool == "undeclared_park"

    asyncio.run(run())
    assert BODY_RUNS["undeclared_park"] == 1


def test_the_client_tool_adapter_returns_a_declared_park_as_today():
    async def run() -> tuple[Any, Any]:
        async with app.app_context(_manifest()):
            token = set_resume_continuation_tool("agent_resume")
            try:
                parked = await (await _client_handle("declared_park")).ainvoke({"q": "x"})
                buffered = await (await _client_handle("declared_buffered")).ainvoke({"q": "x"})
            finally:
                reset_resume_continuation_tool(token)
            return parked, buffered

    parked, buffered = asyncio.run(run())
    assert read_suspended_interaction_marker(parked) is not None
    assert isinstance(buffered, ResumeBuffered)


# --- the answer of app.tools.pauses ---------------------------------------------------------


def test_pauses_answers_for_tools_presets_and_branches():
    extensions = {
        "declared_park": [["plainwrap"]],
        "undeclared_park": [["plainwrap"], ["relaywrap"]],
    }

    async def run() -> dict[str, bool]:
        async with app.app_context(_manifest(extensions)):
            await app.preset_manager.register("p1", "declared_park", {}, [], "d")
            await app.preset_manager.register("p_branched", "declared_park", {}, [["plainwrap"]], "d")
            await app.preset_manager.register("p_over_relay", "undeclared_park_relaywrap", {}, [], "d")
            names = [
                "declared_park",
                "p1",
                "declared_park_plainwrap",
                "p_branched_plainwrap",
                "undeclared_park_relaywrap",
                "p_over_relay",
                "undeclared_park",
                "undeclared_park_plainwrap",
                "echo",
                "no_such_tool",
            ]
            return {name: await app.tools.pauses(name) for name in names}

    answers = asyncio.run(run())
    assert answers == {
        "declared_park": True,
        "p1": True,
        "declared_park_plainwrap": True,
        "p_branched_plainwrap": True,
        "undeclared_park_relaywrap": True,
        "p_over_relay": True,
        "undeclared_park": False,
        "undeclared_park_plainwrap": False,
        "echo": False,
        "no_such_tool": False,
    }


def test_the_registry_is_empty_after_a_boot_reset():
    async def run() -> None:
        async with app.app_context(_manifest()):
            assert "declared_park" in app._tool_pause_registry
        async with app.app_context(Manifest.model_validate({})):
            assert "declared_park" not in app._tool_pause_registry

    asyncio.run(run())
