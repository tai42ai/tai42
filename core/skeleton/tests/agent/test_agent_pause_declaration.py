"""An agent run tool's pause declaration comes from the kind's ``parks`` class attribute alone.

A kind with ``parks = True`` registers its run tool as pausing — for the run tool, a branch of it
and a preset over it — and its park passes through ``run_tool``, through a preset over it, and over
the MCP edge, where the run tool and a preset over it answer the reserved park marker and the runs
index records ``parked``. A kind keeping ``parks = False`` registers no declaration, so a park its
run returns is refused as undeclared through ``run_tool``, through a client-tool handle, through a
preset over it and over the MCP edge. A registration whose ``meta`` names the pause key itself is
refused: the declaration has one source.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from mcp.types import TextContent
from tai42_contract.interactions import SuspendedInteraction, read_suspended_interaction_marker
from tai42_contract.tools import TOOL_META_PAUSES, UndeclaredPauseError

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import CorePluginBootError

_MODULE = "tests.agent._pause_declaration_fixtures"


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "extensions_modules": ["tests.app._fixtures.ext_mod"],
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_b",
                    "include": ["shout"],
                    "extensions": {"parking_kind": [["loud"]]},
                }
            ],
            "agents": [{"title": "kinds", "module": _MODULE, "include": ["parking_kind", "still_kind"]}],
        }
    )


def test_a_parking_kind_declares_its_run_tool_branch_and_preset(preset_manager_restored: Any) -> None:
    async def run() -> dict[str, bool]:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("parking_preset", "parking_kind", {}, [], "A preset over the kind.")
            names = ["parking_kind", "parking_kind_loud", "parking_preset", "still_kind"]
            return {name: await app.tools.pauses(name) for name in names}

    assert asyncio.run(run()) == {
        "parking_kind": True,
        "parking_kind_loud": True,
        "parking_preset": True,
        "still_kind": False,
    }


def test_a_parking_kinds_park_passes_through_run_tool() -> None:
    async def run() -> Any:
        async with app.app_context(_manifest()):
            return await app.tools.run_tool("parking_kind", {"text": "hi"})

    result = asyncio.run(run())
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "parked-run"


def test_a_kind_that_never_parks_has_its_park_refused_in_process() -> None:
    async def run() -> None:
        async with app.app_context(_manifest()):
            with pytest.raises(UndeclaredPauseError) as caught:
                await app.tools.run_tool("still_kind", {"text": "hi"})
            assert caught.value.tool == "still_kind"

            [handle] = await app.tools.get_client_tools(["still_kind"])
            with pytest.raises(UndeclaredPauseError) as caught:
                await handle.invoke({"text": "hi"})
            assert caught.value.tool == "still_kind"

    asyncio.run(run())


def test_a_parking_kinds_park_crosses_the_mcp_edge_as_the_park_marker() -> None:
    async def run() -> Any:
        async with app.app_context(_manifest()), Client(app._fast_mcp) as client:
            return await client.call_tool("parking_kind", {"text": "hi"})

    result = asyncio.run(run())
    assert result.is_error is False
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "parked-run"


def test_a_kind_that_never_parks_has_its_park_refused_at_the_mcp_edge() -> None:
    async def run() -> Any:
        async with app.app_context(_manifest()), Client(app._fast_mcp) as client:
            return await client.call_tool("still_kind", {"text": "hi"}, raise_on_error=False)

    result = asyncio.run(run())
    assert result.is_error is True
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    assert texts == [str(UndeclaredPauseError("still_kind"))]


def test_a_parking_kinds_park_passes_through_a_preset_over_it(preset_manager_restored: Any) -> None:
    async def run() -> Any:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("parking_preset", "parking_kind", {}, [], "A preset over the kind.")
            return await app.tools.run_tool("parking_preset", {"text": "hi"})

    result = asyncio.run(run())
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "parked-run"


def test_a_preset_over_a_kind_that_never_parks_has_its_park_refused(preset_manager_restored: Any) -> None:
    async def run() -> None:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("still_preset", "still_kind", {}, [], "A preset over the kind.")
            with pytest.raises(UndeclaredPauseError) as caught:
                await app.tools.run_tool("still_preset", {"text": "hi"})
            assert caught.value.tool == "still_preset"

    asyncio.run(run())


def test_a_preset_over_a_parking_kind_records_parked_at_the_mcp_edge(
    preset_manager_restored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tai42_skeleton.runs.chokepoint as chokepoint

    terminals: list[dict[str, Any]] = []

    class _SpyStore:
        async def insert_start(
            self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
        ):
            pass

        async def update_outcome(
            self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None, resumed_interactions=None
        ):
            terminals.append({"outcome": outcome, "interaction_id": interaction_id})

    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _SpyStore())

    async def run() -> Any:
        async with app.app_context(_manifest()):
            await app.preset_manager.register("parking_preset", "parking_kind", {}, [], "A preset over the kind.")
            async with Client(app._fast_mcp) as client:
                return await client.call_tool("parking_preset", {"text": "hi"})

    result = asyncio.run(run())
    assert terminals == [{"outcome": "parked", "interaction_id": "parked-run"}]
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "parked-run"


def test_a_preset_declaring_an_output_schema_over_a_parking_kind_parks_over_the_mcp_edge(
    preset_manager_restored: Any,
) -> None:
    # A preset with an ``output_schema`` over an agent forces the agent's structured output and
    # advertises the authored schema; the edge advertises the park marker beside it, so the kind's
    # park reaches the MCP caller as the marker instead of a schema refusal.
    answer_schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

    async def run() -> Any:
        async with app.app_context(_manifest()):
            await app.preset_manager.register(
                "forced_parking_preset",
                "parking_kind",
                {},
                [],
                "A forced preset over the kind.",
                output_schema=answer_schema,
            )
            async with Client(app._fast_mcp) as client:
                return await client.call_tool("forced_parking_preset", {"text": "hi"}, raise_on_error=False)

    result = asyncio.run(run())
    assert result.is_error is False, result.content
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None
    assert marker["interaction_id"] == "parked-run"


def test_a_registration_meta_naming_the_pause_key_is_refused() -> None:
    manifest = Manifest.model_validate(
        {"agents": [{"title": "bad", "module": "tests.agent._meta_pause_fixtures", "include": ["meta_pause_kind"]}]}
    )

    async def run() -> None:
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.raises(CorePluginBootError, match=rf"class attribute 'parks', not with meta\['{TOOL_META_PAUSES}'\]"):
        asyncio.run(run())
