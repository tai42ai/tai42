"""The preset DELETE gate's referee consult, driven at the operations layer.

A registered delete referee whose answer for the name is non-empty BLOCKS the delete with
a 409 that NAMES the holder (nothing is torn down); an empty answer (the referee cascaded
its own cleanup, or holds nothing) lets the delete proceed; a referee that RAISES fails the
delete loudly and the preset survives. Generic fixtures only (echo)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import presets as preset_ops

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {"tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}]}


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
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


@pytest.fixture(autouse=True)
def _isolate_referee_registry():
    yield
    instance.app._delete_referee_registry.reset()


async def _create(name: str, base_tool: str = "echo", **over: Any) -> None:
    await preset_ops.create_preset(
        name=name,
        base_tool=base_tool,
        description=over.get("description", "d"),
        fixed_kwargs=over.get("fixed_kwargs", {}),
        extensions=over.get("extensions", []),
        output_schema=over.get("output_schema"),
    )


async def _blocks(_name: str) -> list[str]:
    return ["node 'n1' binding references it"]


async def _allows(_name: str) -> list[str]:
    return []


async def _boom(_name: str) -> list[str]:
    raise RuntimeError("referee store unreadable")


def test_facet_registers_lists_and_rejects_duplicate(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            instance.app._delete_referee_registry.reset()
            instance.app.tools.register_delete_referee(_allows)
            assert instance.app.tools.delete_referees() == [_allows]
            with pytest.raises(ValueError, match="already registered"):
                instance.app.tools.register_delete_referee(_allows)

    asyncio.run(run())


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
