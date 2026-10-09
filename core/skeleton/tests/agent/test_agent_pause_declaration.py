"""An agent run tool's pause declaration comes from the kind's ``parks`` class attribute alone.

A kind with ``parks = True`` registers its run tool as pausing — for the run tool, a branch of it
and a preset over it — and its park passes through ``run_tool``. A kind keeping ``parks = False``
registers no declaration, so a park its run returns is refused as undeclared through ``run_tool``
and through a client-tool handle. A
registration whose ``meta`` names the pause key itself is refused: the declaration has one source.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.interactions import SuspendedInteraction
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


def test_a_registration_meta_naming_the_pause_key_is_refused() -> None:
    manifest = Manifest.model_validate(
        {"agents": [{"title": "bad", "module": "tests.agent._meta_pause_fixtures", "include": ["meta_pause_kind"]}]}
    )

    async def run() -> None:
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.raises(CorePluginBootError, match=rf"class attribute 'parks', not with meta\['{TOOL_META_PAUSES}'\]"):
        asyncio.run(run())
