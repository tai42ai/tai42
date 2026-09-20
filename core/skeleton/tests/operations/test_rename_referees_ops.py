"""The rename gate's referee union and the ``GET /referees`` preview door, driven at
the operations layer.

A registered referee (a plugin holder, or a platform-internal wiring surface) whose
answer for the OLD name is non-empty BLOCKS the rename with a 409 that NAMES the holder;
empty answers let the rename proceed; a referee that RAISES fails the rename loudly and
the rename is never applied. The preview door returns the SAME union — the referencing
presets (the preset-body leg) plus every registered referee's descriptions. A successful
rename still MOVES the tool_meta overlay.

The registered referees here stand in for the union's provider leg; the platform-internal
referees' own live-reference behavior is pinned directly in
``tests/tools/test_rename_referees.py``. Generic fixtures only (echo / alerts).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.presets import PresetBody
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import presets as preset_ops

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "tools": [
        {
            "title": "fx",
            "module": "tests.presets._fixtures",
            "include": ["weather", "echo"],
        }
    ],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def pg(monkeypatch) -> FakeVersioningPg:
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    return fake


@pytest.fixture(autouse=True)
def _reset_preset_registry():
    """Tear down every runtime-registered / quarantined preset after each test — the
    singleton ``PresetManager`` outlives one ``app_context``."""
    yield
    mgr = instance.app.preset_manager

    async def _clear() -> None:
        for name in list(mgr.registered_names()):
            await mgr.remove(name)
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


@pytest.fixture(autouse=True)
def _isolate_referee_registry():
    """The referee collection lives on the app singleton and is re-armed with the
    platform-internal referees on each ``start()``. Clear it after every test so a
    test-registered provider never leaks into another suite that does not re-boot."""
    yield
    instance.app._rename_referee_registry.reset()


async def _create(name: str, base_tool: str = "echo", **over: Any) -> None:
    await preset_ops.create_preset(
        name=name,
        base_tool=base_tool,
        description=over.get("description", "d"),
        fixed_kwargs=over.get("fixed_kwargs", {}),
        extensions=over.get("extensions", []),
        output_schema=over.get("output_schema"),
    )


async def _seed_body(name: str, base_tool: str, fixed_kwargs: dict[str, Any]) -> None:
    body = PresetBody(base_tool=base_tool, description="d", fixed_kwargs=fixed_kwargs, extensions=[])
    await instance.app.versioning.store.create("preset", name, body.model_dump())


async def _echo_holder(_name: str) -> list[str]:
    return ["schedule 'echo' fires it"]


async def _no_holders(_name: str) -> list[str]:
    return []


async def _boom_referee(_name: str) -> list[str]:
    raise RuntimeError("referee store unreadable")


# -- facet seam (through a live app) ------------------------------------------


def test_facet_registers_lists_and_rejects_duplicate(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            instance.app.tools.register_rename_referee(_echo_holder)
            # The gate/preview consume the collection through this same accessor.
            assert instance.app.tools.rename_referees() == [_echo_holder]
            # A double registration of the same provider is a loud bug, never a silent
            # duplicate consult.
            with pytest.raises(ValueError, match="already registered"):
                instance.app.tools.register_rename_referee(_echo_holder)

    asyncio.run(run())


# -- registered referee blocks / proceeds / raises ---------------------------


def test_registered_referee_nonempty_blocks_rename_and_names_holder(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            instance.app.tools.register_rename_referee(_echo_holder)
            await _create("alerts")

            with pytest.raises(ConflictError, match="cannot be renamed") as ei:
                await preset_ops.rename_preset(name="alerts", new_name="alerts2")
            # The 409 NAMES the holder the referee reported.
            assert "schedule 'echo' fires it" in str(ei.value)
            # The rename was not applied — the preset stays live under its old name.
            assert instance.app.preset_manager.is_registered("alerts")
            assert not instance.app.preset_manager.is_registered("alerts2")

    asyncio.run(run())


def test_empty_referee_answers_let_rename_proceed(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            instance.app.tools.register_rename_referee(_no_holders)
            await _create("alerts")

            result = await preset_ops.rename_preset(name="alerts", new_name="alerts2")
            assert result["renamed_from"] == "alerts"
            assert result["name"] == "alerts2"
            assert instance.app.preset_manager.is_registered("alerts2")
            assert not instance.app.preset_manager.is_registered("alerts")

    asyncio.run(run())


def test_referee_that_raises_fails_rename_loudly(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            instance.app.tools.register_rename_referee(_boom_referee)
            await _create("alerts")

            # The referee's exception propagates — never swallowed into a silent proceed.
            with pytest.raises(RuntimeError, match="referee store unreadable"):
                await preset_ops.rename_preset(name="alerts", new_name="alerts2")
            # And the rename never happened: the old binding is intact, the new absent.
            assert instance.app.preset_manager.is_registered("alerts")
            assert not instance.app.preset_manager.is_registered("alerts2")
            assert (await instance.app.presets.store.get_preset("alerts")).name == "alerts"

    asyncio.run(run())


# -- preview door returns the union ------------------------------------------


def test_preview_door_returns_union_of_presets_and_referees(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._rename_referee_registry.reset()
            instance.app.tools.register_rename_referee(_echo_holder)
            # The preset-body leg: another preset whose active body composes ``leaf``.
            await _seed_body("leaf", "weather", {"units": "v"})
            await _seed_body("composer", "echo", {"tool_names": ["leaf"]})

            referees = (await preset_ops.preset_referees(name="leaf"))["referees"]
            # The full union: the referencing preset (preset-body leg) PLUS the registered
            # referee's description.
            assert referees == ["composer", "schedule 'echo' fires it"]

    asyncio.run(run())


# -- tool_meta moves on a successful rename ----------------------------------


def test_tool_meta_moves_on_successful_rename(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # No referees object, so the rename proceeds and re-keys the overlay.
            instance.app._rename_referee_registry.reset()
            await _create("alerts")
            await instance.app.tool_meta.store.upsert_meta(
                "alerts", display_name="Alerts", folder_id=None, tags=["x"], hidden=None
            )

            await preset_ops.rename_preset(name="alerts", new_name="alerts2")

            moved = await instance.app.tool_meta.store.get_meta("alerts2")
            assert moved is not None
            assert moved.display_name == "Alerts"
            assert moved.tags == ["x"]
            # The overlay no longer sits under the old name.
            assert await instance.app.tool_meta.store.get_meta("alerts") is None

    asyncio.run(run())
