"""Preset reconciliation across scoped MCP reload / deregister.

Drives the REAL app (``instance.app``) with a faked MCP probe so an MCP server
binds a base tool a preset is then built over, and exercises the three
interactions the reconciliation seam covers:

* ``reload_mcp`` rebinds the base — the dependent preset is re-registered from its
  spec so its ``TransformedTool`` tracks the freshly-bound base (not the stale
  pre-reload one);
* ``deregister_mcp`` removes the base — a dependent preset is quarantined
  (``conflicted``);
* a returning MCP server whose tool name a stored preset owns is REFUSED (the preset
  is never clobbered), and the reload result surfaces the conflict.

It also drives the binder's stored-preset rule at boot, the failed-server record of a
preset store that cannot be read, and the schema gate at the head of an epoch build.

The store is the stateful in-memory fake (the ``pg`` fixture); the probe is mocked,
so no network or live database is touched.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
import tai42_kit.db.migrations as kit_migrations
from tai42_contract.agent.base import PresetSpec
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.presets import PresetBody
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import ComponentStatus, MigrationScript

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.db import boot_gate
from tai42_skeleton.db.boot_gate import SchemaOutOfDateError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.presets.errors import PresetStoreReadError

from ..versioning.conftest import FakeVersioningPg
from ._fixtures.reload import reload_with


class _FakeMcpTool:
    name = "ping"
    description = "ping"
    inputSchema: ClassVar[dict] = {"type": "object", "properties": {}}
    outputSchema: ClassVar[dict] = {}
    title = None
    annotations = None
    icons = None


def _cfg(title: str = "svc") -> TaiMCPConfig:
    return TaiMCPConfig(title=title, include=[], config=MCPConfig(type="http", url="http://x/mcp"))


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
            "mcp": [_cfg("svc").model_dump()],
        }
    )


@pytest.fixture
def pg(monkeypatch) -> FakeVersioningPg:
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    # Signal a store-configured deployment (the gate reconcile + rehydrate consult):
    # a versioned preset can only exist when the store is wired up, so faking the
    # store transport must also configure the skeleton database.
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


# Reload/deregister are driven through the reload gate — the real production path
# (``routers/manifest`` and the builtin MCP tool call them exactly this way). The
# gate runs the sync admin facet on a worker thread, so the serving loop keeps
# spinning and the preset reconcile the facet marshals back onto it can run. A
# direct call from this on-loop coroutine would freeze the serving loop and
# deadlock (the facet raises loudly on that misuse).
async def _reload_mcp(title: str) -> dict[str, Any]:
    return await reload_gate.run(lambda: instance.app.admin.reload_mcp(title), reimports=False)


async def _deregister_mcp(title: str) -> dict[str, Any]:
    return await reload_gate.run(lambda: instance.app.admin.deregister_mcp(title), reimports=False)


async def _register_versioned(name: str, base_tool: str) -> None:
    await instance.app.presets.store.create_preset(
        PresetSpec(name=name, description="d", base_tool=base_tool, fixed_kwargs={}), extensions=[]
    )
    body = await instance.app.presets.store.get_active_body(name)
    await instance.app.preset_manager.register(name, body.base_tool, body.fixed_kwargs, [], body.description)


def test_reload_mcp_reregisters_dependent_preset_over_new_base(pg, monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest()):
            assert "svc_ping" in await instance.app.tools.get_tools()
            # A preset over the MCP base tool.
            await instance.app.preset_manager.register("myp", "svc_ping", {}, [], "d")
            old_parent = (await instance.app.tools.get_tool("myp")).parent_tool  # type: ignore[attr-defined]

            await _reload_mcp("svc")

            # The preset survives AND its transform now tracks the freshly-bound base
            # (the reconciliation re-registered it from spec, not the stale closure).
            assert instance.app.preset_manager.is_registered("myp")
            new_base = await instance.app.tools.get_tool("svc_ping")
            new_parent = (await instance.app.tools.get_tool("myp")).parent_tool  # type: ignore[attr-defined]
            assert new_parent is new_base
            assert new_parent is not old_parent

    asyncio.run(run())


def test_deregister_mcp_quarantines_dependent_presets(pg, monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest()):
            await _register_versioned("verp", "svc_ping")

            await _deregister_mcp("svc")

            mgr = instance.app.preset_manager
            # The base vanished, so the dependent preset is quarantined — its store
            # row surfaces as ``conflicted`` rather than staying bound to a gone base.
            assert mgr.is_quarantined("verp")
            assert not mgr.is_registered("verp")
            assert "verp" not in await instance.app.tools.get_tools()

    asyncio.run(run())


def test_deregister_store_less_reconcile_opens_no_store(monkeypatch):
    # A store-less deployment (the skeleton database unconfigured): reconcile
    # rebinds/quarantines from the in-memory spec map alone and NEVER opens the
    # versioned Postgres store.
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    @asynccontextmanager
    async def forbid_client_ctx(client_cls, settings=None, **kwargs):
        raise AssertionError("versioned store opened in a store-less deployment")
        yield  # pragma: no cover - unreachable, satisfies the context-manager protocol

    monkeypatch.setattr(store_module, "client_ctx", forbid_client_ctx)

    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest()):
            # A preset over the MCP base tool, bound directly.
            await instance.app.preset_manager.register("depp", "svc_ping", {}, [], "d")

            await _deregister_mcp("svc")

            # The base vanished, so the dependent preset is quarantined — reconcile
            # reads only the in-memory spec map, opening no store connection.
            mgr = instance.app.preset_manager
            assert not mgr.is_registered("depp")
            assert mgr.is_quarantined("depp")
            assert "depp" not in await instance.app.tools.get_tools()

    asyncio.run(run())


def test_reload_mcp_refuses_name_owned_by_registered_preset(pg, monkeypatch):
    async def run():
        # The MCP is DOWN at boot, so ``svc_ping`` is unbound; a preset then takes
        # that name. When the server returns, the (re)bind must REFUSE the name (the
        # preset is never clobbered) and surface the conflict in the result.
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(side_effect=TimeoutError("down")))
        async with instance.app.app_context(_manifest()):
            assert "svc_ping" not in await instance.app.tools.get_tools()
            await _seed("svc_ping", "echo")
            await instance.app.preset_manager.rehydrate()

            instance.app._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])  # type: ignore[method-assign]
            result = await _reload_mcp("svc")

            assert result["preset_conflicts"] == ["svc_ping"]
            assert "svc_ping" not in result["tools"]
            # The preset still owns the name and still binds ``echo``.
            assert instance.app.preset_manager.is_registered("svc_ping")
            assert await instance.app.tools.run_tool("svc_ping", {"text": "hi"}) == "hi"

    asyncio.run(run())


async def _seed(name: str, base_tool: str) -> None:
    """Persist a preset through the store, the shape the create door writes."""
    body = PresetBody(base_tool=base_tool, description="d", fixed_kwargs={}, extensions=[])
    await instance.app.versioning.store.create("preset", name, body.model_dump())


def _manifest_without_echo() -> Manifest:
    return Manifest.model_validate(
        {
            "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather"]}],
            "mcp": [_cfg("svc").model_dump()],
        }
    )


def _raise_on_population_read(monkeypatch) -> None:
    async def broken(self):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(type(instance.app.presets), "list_active_versioned_bodies", broken)


def test_boot_never_binds_an_mcp_tool_under_a_stored_preset_name(pg, monkeypatch):
    # A stored preset owns ``svc_ping`` before the server serving ``ping`` boots: the
    # binder skips the name, so the preset registers over ``echo`` on every worker
    # whatever the order of boots and binds.
    async def run():
        await _seed("svc_ping", "echo")
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest()):
            mgr = instance.app.preset_manager
            assert mgr.is_registered("svc_ping")
            assert not mgr.is_quarantined("svc_ping")
            assert await instance.app.tools.run_tool("svc_ping", {"text": "hi"}) == "hi"
            assert instance.app._mcp_preset_conflicts["svc"] == {"svc_ping"}
            assert "svc_ping" not in instance.app._mcp_bound_tools["svc"]

    asyncio.run(run())


def test_boot_quarantines_a_stored_preset_on_its_missing_base_not_on_the_mcp_name(pg, monkeypatch):
    async def run():
        await _seed("svc_ping", "echo")
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest_without_echo()):
            mgr = instance.app.preset_manager
            assert mgr.is_quarantined("svc_ping")
            assert mgr.quarantine_reason("svc_ping") == "its base tool 'echo' is not a registered tool"
            assert "svc_ping" not in await instance.app.tools.get_tools()

    asyncio.run(run())


def test_reload_mcp_raises_on_an_unreadable_preset_store_and_touches_nothing(pg, monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest()):
            bound = set(instance.app._mcp_bound_tools["svc"])
            assert bound == {"svc_ping"}
            _raise_on_population_read(monkeypatch)

            with pytest.raises(PresetStoreReadError, match="the preset store could not be read: connection refused"):
                await _reload_mcp("svc")

            assert instance.app._mcp_bound_tools["svc"] == bound
            assert "svc_ping" in await instance.app.tools.get_tools()
            assert "svc" not in instance.app._failed_mcps

    asyncio.run(run())


def test_boot_fails_on_an_unreadable_preset_store(pg, monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        _raise_on_population_read(monkeypatch)
        with pytest.raises(PresetStoreReadError, match="the preset store could not be read"):
            async with instance.app.app_context(_manifest()):
                pass

    asyncio.run(run())


async def _reload_failed() -> list[dict[str, Any]]:
    return await reload_gate.run(lambda: instance.app.admin.reload_failed_mcps(), reimports=False)


def _assert_failed_on_the_preset_store() -> None:
    [record] = [row for row in instance.app.admin.list_failed_mcps() if row["title"] == "svc"]
    assert record["category"] == "preset_store"
    assert record["message"].startswith("the preset store could not be read:")


def test_failed_server_batch_records_an_unreadable_preset_store(pg, monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(side_effect=TimeoutError("down")))
        async with instance.app.app_context(_manifest()):
            assert "svc" in instance.app._failed_mcps
            instance.app._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])  # type: ignore[method-assign]
            _raise_on_population_read(monkeypatch)

            assert await _reload_failed() == [{"title": "svc", "status": "error"}]
            _assert_failed_on_the_preset_store()
            assert "svc_ping" not in await instance.app.tools.get_tools()

    asyncio.run(run())


def test_reprobe_pass_records_an_unreadable_preset_store(pg, monkeypatch):
    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(side_effect=TimeoutError("down")))
        async with instance.app.app_context(_manifest()):
            instance.app._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])  # type: ignore[method-assign]
            _raise_on_population_read(monkeypatch)

            assert await instance.app._reprobe_failed_titles(["svc"]) == [{"title": "svc", "status": "error"}]
            _assert_failed_on_the_preset_store()

    asyncio.run(run())


def test_no_skeleton_database_opens_no_store_and_binds_every_served_name(monkeypatch):
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    @asynccontextmanager
    async def forbid_client_ctx(client_cls, settings=None, **kwargs):
        raise AssertionError("versioned store opened with no skeleton database")
        yield  # pragma: no cover - unreachable, satisfies the context-manager protocol

    monkeypatch.setattr(store_module, "client_ctx", forbid_client_ctx)

    async def run():
        monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))
        async with instance.app.app_context(_manifest()):
            assert instance.app._mcp_bound_tools["svc"] == {"svc_ping"}
            result = await _reload_mcp("svc")
            assert result["tools"] == ["svc_ping"]
            assert "preset_conflicts" not in result

    asyncio.run(run())


def _pending_skeleton_chain(monkeypatch) -> None:
    """The real schema gate over a configured skeleton database whose chain has a pending migration."""
    monkeypatch.setattr(boot_gate, "component_store_configured", lambda component: True)
    monkeypatch.setattr(boot_gate, "component_store_settings", lambda component: SimpleNamespace(pg_dsn="dsn"))
    pending = MigrationScript(version=2, name="add_thing", filename="0002_add_thing.sql", checksum="c", sql="")

    async def status(entries):
        return [ComponentStatus(component="skeleton", applied_versions=(1,), pending=(pending,), mismatches=())]

    monkeypatch.setattr(kit_migrations, "migration_status", status)


def _store_spy(monkeypatch) -> list[str]:
    opened: list[str] = []

    async def spy(self):
        opened.append("read")
        return {}

    monkeypatch.setattr(type(instance.app.presets), "list_active_versioned_bodies", spy)
    return opened


_MIGRATE_FIX = "Run 'tai db migrate' to apply the pending migrations, then restart."


def test_boot_refuses_a_pending_migration_before_any_probe_or_store_read(pg, monkeypatch):
    async def run():
        probe = AsyncMock(return_value=[_FakeMcpTool()])
        monkeypatch.setattr(instance.app, "_probe_mcp", probe)
        opened = _store_spy(monkeypatch)
        _pending_skeleton_chain(monkeypatch)
        with pytest.raises(SchemaOutOfDateError) as excinfo:
            async with instance.app.app_context(_manifest()):
                pass
        assert str(excinfo.value).endswith(_MIGRATE_FIX)
        probe.assert_not_called()
        assert opened == []

    asyncio.run(run())


def test_full_reload_refuses_a_pending_migration_and_the_live_epoch_keeps_serving(pg, monkeypatch):
    async def run():
        probe = AsyncMock(return_value=[_FakeMcpTool()])
        monkeypatch.setattr(instance.app, "_probe_mcp", probe)
        async with instance.app.app_context(_manifest()):
            probe.reset_mock()
            opened = _store_spy(monkeypatch)
            _pending_skeleton_chain(monkeypatch)
            with pytest.raises(SchemaOutOfDateError) as excinfo:
                await reload_with(instance.app, _manifest())
            assert str(excinfo.value).endswith(_MIGRATE_FIX)
            probe.assert_not_called()
            assert opened == []
            assert await instance.app.tools.run_tool("echo", {"text": "hi"}) == "hi"
            assert "svc_ping" in await instance.app.tools.get_tools()

    asyncio.run(run())
