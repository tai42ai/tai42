"""The preset DELETE gate's vetoes and cascades, driven at the operations layer.

A registered delete referee whose answer for a removed name is non-empty BLOCKS the delete
with a 409 that NAMES the holder (nothing is torn down, no cascade runs); an empty answer lets
the delete proceed; a referee that RAISES fails the delete loudly and the preset survives. A
delete removes the preset and every extension branch tool bound off it, so every veto, every
cascade and the overlay cleanup cover each of those names. Generic fixtures only (echo).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.presets import PresetBody
from tai42_contract.tools import ReferenceHolder
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.conversations as conversations_pkg
import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.tools.binding.surface import remove_surface_tool

from .._fakes.caller_reach import bound_caller
from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


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
    yield
    mgr = instance.app.preset_manager

    async def _clear() -> None:
        for name in list(mgr.registered_names()):
            await mgr.remove(name)
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(instance.app._fast_mcp, tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


@pytest.fixture(autouse=True)
def _isolate_referee_registry():
    yield
    instance.app._delete_referee_registry.reset()
    instance.app._delete_cascade_registry.reset()


async def _create(name: str, base_tool: str = "echo", **over: Any) -> None:
    await preset_ops.create_preset(
        name=name,
        base_tool=base_tool,
        description=over.get("description", "d"),
        fixed_kwargs=over.get("fixed_kwargs", {}),
        extensions=over.get("extensions", []),
        output_schema=over.get("output_schema"),
    )


_NODE_HOLDER = ReferenceHolder(kind="fixture holder", tag="tools", name="n1", summary="node 'n1' binding references it")


async def _blocks(names: frozenset[str]) -> dict[str, list[ReferenceHolder]]:
    return {name: [_NODE_HOLDER] for name in names}


async def _allows(_names: frozenset[str]) -> dict[str, list[ReferenceHolder]]:
    return {}


async def _boom(_names: frozenset[str]) -> dict[str, list[ReferenceHolder]]:
    raise RuntimeError("referee store unreadable")


class _Route:
    def __init__(self, route_name: str, target_name: str) -> None:
        self.route_name = route_name
        self.target_kind = "tool"
        self.target_name = target_name


class _RoutesManager:
    def __init__(self, *routes: _Route) -> None:
        self._routes = {route.route_name: route for route in routes}

    async def list_routes(self) -> tuple[dict[str, _Route], None]:
        return dict(self._routes), None


def _bind_routes(monkeypatch, *routes: _Route) -> None:
    manager = _RoutesManager(*routes)
    monkeypatch.setattr(conversations_pkg, "get_conversations_manager", lambda: manager)


def test_facet_registers_lists_and_rejects_duplicate(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._delete_referee_registry.reset()
            instance.app.tools.register_delete_referee(_allows)
            assert instance.app.tools.delete_referees() == [_allows]
            with pytest.raises(ValueError, match="already registered"):
                instance.app.tools.register_delete_referee(_allows)

    asyncio.run(run())


@pytest.mark.usefixtures("admin_caller")
def test_a_vetoing_referee_blocks_the_delete_and_names_the_holder(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._delete_referee_registry.reset()
            await _create("p")
            instance.app.tools.register_delete_referee(_blocks)
            with pytest.raises(ConflictError, match="node 'n1' binding references it"):
                await preset_ops.delete_preset("p")
            # Nothing was torn down — the preset is still registered.
            assert instance.app.preset_manager.is_registered("p")

    asyncio.run(run())


def test_an_allowing_referee_lets_the_delete_proceed(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._delete_referee_registry.reset()
            await _create("p")
            instance.app.tools.register_delete_referee(_allows)
            result = await preset_ops.delete_preset("p")
            assert result["deleted"] is True
            assert not instance.app.preset_manager.is_registered("p")

    asyncio.run(run())


def test_a_raising_referee_fails_the_delete_loudly(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._delete_referee_registry.reset()
            await _create("p")
            instance.app.tools.register_delete_referee(_boom)
            with pytest.raises(RuntimeError, match="referee store unreadable"):
                await preset_ops.delete_preset("p")
            assert instance.app.preset_manager.is_registered("p")

    asyncio.run(run())


@pytest.mark.usefixtures("admin_caller")
def test_the_refusal_lists_each_holder_and_carries_them(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p")
            instance.app.tools.register_delete_referee(_blocks)
            with pytest.raises(ConflictError) as caught:
                await preset_ops.delete_preset("p")
            assert caught.value.message == (
                "preset 'p' cannot be deleted — held by:\n"
                "- node 'n1' binding references it\n"
                "remove those references first"
            )
            assert caught.value.extra == {"holders": [_NODE_HOLDER.model_dump()]}

    asyncio.run(run())


def test_a_role_member_refused_delete_counts_the_holders_its_role_cannot_read(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p")
            instance.app.tools.register_delete_referee(_blocks)
            with pytest.raises(ConflictError) as caught:
                await preset_ops.delete_preset("p")
            assert caught.value.message == (
                "preset 'p' cannot be deleted — held by:\n"
                "- fixture holder: 1 not readable with your role\n"
                "ask someone with access to 'tools' to remove those references first"
            )
            assert caught.value.extra == {
                "holders": [
                    {
                        "kind": "fixture holder",
                        "tag": "tools",
                        "name": "tools",
                        "locator": {"count": 1},
                        "summary": "fixture holder: 1 not readable with your role",
                    }
                ]
            }
            assert instance.app.preset_manager.is_registered("p")

    with bound_caller(monkeypatch, {"presets": "write"}):
        asyncio.run(run())


def test_a_referee_answering_anything_but_holders_fails_the_delete_naming_it(pg) -> None:
    async def _strings(names: frozenset[str]) -> dict[str, list[str]]:
        return {name: ["a plain line"] for name in names}

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p")
            instance.app.tools.register_delete_referee(_strings)  # type: ignore[arg-type]
            with pytest.raises(TypeError, match="_strings"):
                await preset_ops.delete_preset("p")
            assert instance.app.preset_manager.is_registered("p")

    asyncio.run(run())


@pytest.mark.usefixtures("admin_caller")
def test_refused_delete_runs_no_cleanup(pg, monkeypatch) -> None:
    cleaned: list[str] = []

    async def _cleanup(name: str) -> None:
        cleaned.append(name)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p")
            instance.app.tools.register_delete_cascade(_cleanup)
            _bind_routes(monkeypatch, _Route("r1", "p"))
            with pytest.raises(ConflictError, match="conversation route 'r1'"):
                await preset_ops.delete_preset("p")
            assert cleaned == []
            assert instance.app.preset_manager.is_registered("p")

    asyncio.run(run())


def test_an_allowed_delete_runs_each_cascade_once_before_the_record_is_removed(pg) -> None:
    seen: list[tuple[str, bool]] = []

    async def _cleanup(name: str) -> None:
        stored = {record.name for record in await instance.app.presets.store.list_presets()}
        seen.append((name, name in stored))

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p")
            instance.app.tools.register_delete_cascade(_cleanup)
            assert (await preset_ops.delete_preset("p"))["deleted"] is True
            assert seen == [("p", True)]

    asyncio.run(run())


def test_a_raising_cascade_fails_the_delete_and_keeps_the_preset(pg) -> None:
    async def _cleanup_boom(_name: str) -> None:
        raise RuntimeError("cleanup store unreadable")

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p")
            instance.app.tools.register_delete_cascade(_cleanup_boom)
            with pytest.raises(RuntimeError, match="cleanup store unreadable"):
                await preset_ops.delete_preset("p")
            assert instance.app.preset_manager.is_registered("p")
            assert (await instance.app.presets.store.get_preset("p")).name == "p"

    asyncio.run(run())


@pytest.mark.usefixtures("admin_caller")
def test_a_veto_holding_a_branch_of_the_preset_blocks_the_delete(pg) -> None:
    asked: list[frozenset[str]] = []
    branch_holder = ReferenceHolder(
        kind="fixture holder", tag="tools", name="relay", summary="fixture holder 'relay' names 'alerts_exta'"
    )

    async def _branch_veto(names: frozenset[str]) -> dict[str, list[ReferenceHolder]]:
        asked.append(names)
        return {name: [branch_holder] for name in names if name == "alerts_exta"}

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("alerts", extensions=[["exta"]])
            instance.app.tools.register_delete_referee(_branch_veto)
            with pytest.raises(ConflictError) as caught:
                await preset_ops.delete_preset("alerts")
            assert caught.value.message == (
                "preset 'alerts' cannot be deleted — held by:\n"
                "- fixture holder 'relay' names 'alerts_exta'\n"
                "remove those references first"
            )
            # The veto is asked once, for the preset and its branch together.
            assert asked == [frozenset({"alerts", "alerts_exta"})]
            assert instance.app.preset_manager.is_registered("alerts")
            assert "alerts_exta" in await instance.app.tools.get_tools()

    asyncio.run(run())


def test_every_delete_cascade_runs_for_the_preset_and_each_branch(pg) -> None:
    first: list[str] = []
    second: list[str] = []

    async def _first(name: str) -> None:
        first.append(name)

    async def _second(name: str) -> None:
        second.append(name)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("alerts", extensions=[["exta"]])
            instance.app.tools.register_delete_cascade(_first)
            instance.app.tools.register_delete_cascade(_second)
            await preset_ops.delete_preset("alerts")
            assert first == ["alerts", "alerts_exta"]
            assert second == ["alerts", "alerts_exta"]

    asyncio.run(run())


def test_the_delete_clears_the_overlay_row_of_the_preset_and_each_branch(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("alerts", extensions=[["exta"]])
            meta = instance.app.tool_meta.store
            for name in ("alerts", "alerts_exta"):
                await meta.upsert_meta(name, display_name=name, folder_id=None, tags=[], hidden=True)
            await preset_ops.delete_preset("alerts")
            assert await meta.get_meta("alerts") is None
            assert await meta.get_meta("alerts_exta") is None

    asyncio.run(run())


def test_a_quarantined_delete_cascades_and_clears_its_own_name_only(pg) -> None:
    cleaned: list[str] = []

    async def _cleanup(name: str) -> None:
        cleaned.append(name)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A stored preset whose name is a live tool rehydrates into quarantine; the live tool's
            # own branches are not the record's.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await instance.app.versioning.store.create("preset", "weather", body.model_dump())
            await instance.app.preset_manager.rehydrate()
            assert instance.app.preset_manager.is_quarantined("weather")
            meta = instance.app.tool_meta.store
            for name in ("weather", "weather_exta"):
                await meta.upsert_meta(name, display_name=name, folder_id=None, tags=[], hidden=True)
            instance.app.tools.register_delete_cascade(_cleanup)
            await preset_ops.delete_preset("weather")
            assert cleaned == ["weather"]
            assert await meta.get_meta("weather") is None
            assert await meta.get_meta("weather_exta") is not None
            assert not instance.app.preset_manager.is_quarantined("weather")

    asyncio.run(run())
