"""``start()``/reload boot path: the registry-name snapshot, tool loading, cross-update
idempotence, connector registration at boot, and the kind-status startup summary."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app import kind_status as ks
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import CorePluginBootError
from tai42_skeleton.monitoring.registry import reset_monitoring

from .._fixtures.reload import reload_with
from ._doubles import _Mixin

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from tai42_skeleton.template import ResourceManager

_BOOT_LOGGER = "tai42_skeleton.app.lifecycle.boot"


def test_registry_names_sync_on_empty_server():
    m = _Mixin()
    assert m._registry_names_sync()["tool"] == set()


def test_registry_names_sync_raises_when_list_tools_fails():
    # A failing list_tools() must propagate through the off-loop runner — never
    # leave the caller blocked.
    m = _Mixin()
    assert m._building is not None
    m._building._fast_mcp = cast(
        "FastMCP",
        MagicMock(
            list_tools=AsyncMock(side_effect=RuntimeError("list_tools boom")),
            list_prompts=AsyncMock(return_value=[]),
            list_resources=AsyncMock(return_value=[]),
        ),
    )
    with pytest.raises(RuntimeError, match="list_tools boom"):
        m._registry_names_sync()


def test_start_binds_global_handle_and_loads_tools():
    manifest = Manifest.model_validate(
        {"tools": [{"title": "fxt", "module": "tests.app._fixtures.tools_a", "include": ["greet"]}]}
    )

    async def run():
        async with app.app_context(manifest):
            # start() claims the global handle, binding it to this app impl.
            assert object.__getattribute__(tai42_app, "_impl") is app
            tools = await app.tools.get_tools()
            assert "greet" in tools

    asyncio.run(run())


def test_start_clears_cached_resource_manager():
    # A reload re-imports the storage module and rebuilds the storage provider;
    # start() must drop the cached resource manager so it cannot keep rendering
    # against (and pinning open) the previous provider.
    async def run():
        app._resource_manager_cache = cast("ResourceManager", "stale")
        async with app.app_context(Manifest.model_validate({})):
            assert app._resource_manager_cache is None

    asyncio.run(run())


def test_reload_starts_from_a_cold_template_cache():
    # Fleet-reload parity for the compiled-template cache: a config reload rebuilds the
    # resource manager (start() drops the cache), so its compiled-template cache starts
    # cold on every worker that applies the reload — a stale compilation never outlives
    # the config it was rendered under. The per-key ``evict_template`` broadcast covers a
    # single upload; a full reload cold-starts the whole cache through this same reset,
    # so no separate reload hook is registered (that would clear a manager about to be
    # discarded).
    manifest = Manifest.model_validate({})

    async def run():
        async with app.app_context(manifest):
            warm = app.storage.resource_manager  # builds + caches a manager
            assert app._resource_manager_cache is warm
            await reload_with(app, manifest)
            # The reload dropped the cache; the next access builds a fresh, cold manager.
            assert app._resource_manager_cache is None
            assert app.storage.resource_manager is not warm

    asyncio.run(run())


def test_module_handlers_and_middleware_idempotent_across_update():
    # A lifecycle module registers a startup/shutdown handler + a middleware on
    # import. Each start()/update() re-imports it and re-fires the decorators;
    # the qualname-keyed registries must keep the counts at exactly one, never
    # 1->2->3 (which would re-run shutdowns N+1 times and duplicate middleware).
    manifest = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.lifecycle_reg"]})

    def _counts() -> tuple[int, int, int]:
        startups = sum(1 for k in app._startup_handlers if k.endswith(".startup_marker"))
        shutdowns = sum(1 for k in app._shutdown_handlers if k.endswith(".shutdown_marker"))
        middlewares = sum(1 for k in app._http_surface._middlewares if k.endswith(".MarkerMiddleware"))
        return startups, shutdowns, middlewares

    async def run():
        async with app.app_context(manifest):
            assert _counts() == (1, 1, 1)
            for _ in range(3):
                await reload_with(app, manifest)
                assert _counts() == (1, 1, 1)

    asyncio.run(run())


def test_update_drops_old_tools_and_reruns_reload_handlers():
    base = Manifest.model_validate(
        {"tools": [{"title": "fxt", "module": "tests.app._fixtures.tools_a", "include": ["greet"]}]}
    )
    empty = Manifest.model_validate({})
    reloaded: list[bool] = []

    async def run():
        async with app.app_context(base):

            @app.lifecycle.on_reload
            def _mark():
                reloaded.append(True)

            assert "greet" in await app.tools.get_tools()
            await reload_with(app, empty)
            # The greet tool is gone after re-init to an empty manifest.
            assert "greet" not in await app.tools.get_tools()
            # The reload handler ran during update().
            assert reloaded

    asyncio.run(run())


def test_failed_update_leaves_previous_tool_set_live():
    # A reload to a manifest whose SCALAR slot is broken must fail loudly (a
    # scalar slot never quarantines — the server cannot run without it), but the
    # worker's previous tool surface is restored (re-added) rather than left
    # empty — a bad module bricks nothing.
    base = Manifest.model_validate(
        {"tools": [{"title": "fxt", "module": "tests.app._fixtures.tools_a", "include": ["greet"]}]}
    )
    broken = Manifest.model_validate({"storage_module": "totally_bogus_pkg_xyz"})

    async def run():
        async with app.app_context(base):
            assert "greet" in await app.tools.get_tools()
            with pytest.raises(CorePluginBootError, match="totally_bogus_pkg_xyz"):
                await reload_with(app, broken)
            # The previous tool surface is restored after the failed reload.
            assert "greet" in await app.tools.get_tools()

    asyncio.run(run())


def test_reload_with_connector_plugin_is_reload_safe():
    # A manifest carrying a connector plugin module: start() imports it, running
    # register_connector(...). update() re-imports the same module — without the
    # start()-time registry reset the duplicate guard would crash the reload.
    from tai42_skeleton.connectors.providers import registry as conn_registry

    provider_id = "fixture_conn"
    manifest = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.connector_plugin"]})

    saved = dict(conn_registry._REGISTRY)
    conn_registry._REGISTRY.clear()

    async def run():
        async with app.app_context(manifest):
            assert conn_registry.get_provider(provider_id).id == provider_id
            # Reload re-imports the plugin; the registry reset makes the repeated
            # register_connector(...) safe instead of a duplicate-id crash.
            await reload_with(app, manifest)
            assert conn_registry.get_provider(provider_id).id == provider_id

    try:
        asyncio.run(run())
    finally:
        conn_registry._REGISTRY.clear()
        conn_registry._REGISTRY.update(saved)


def _oauth_descriptor(provider_id: str = "acme"):
    """An http-transport oauth ProviderDescriptor for the manifest ``connectors``
    boot/reload tests — synthetic, not a shipped connector."""
    from tai42_contract.connectors.providers import (
        McpServerDescriptor,
        OAuthEndpoints,
        ProviderDescriptor,
        SubServiceDescriptor,
    )

    return ProviderDescriptor(
        id=provider_id,
        display_name="Acme",
        icon_url="https://acme.test/icon.png",
        kind="oauth",
        origin="system",
        category="productivity",
        oauth=OAuthEndpoints(authorize="https://acme.test/authorize", token="https://acme.test/token"),
        client_id_env="ACME_CLIENT_ID",
        client_secret_env="ACME_CLIENT_SECRET",
        sub_services={
            "mail": SubServiceDescriptor(
                id="mail",
                display_name="Mail",
                scopes=["mail.read"],
                mcp_server=McpServerDescriptor(type="http", url="https://acme.test/mcp/mail"),
            ),
        },
    )


def test_manifest_connectors_registered_at_boot():
    # A manifest ``connectors`` entry is registered during boot through the
    # ``tai42_app.connectors.register_connector`` facet — no import side effect —
    # so the committed catalog lists it once the app context is up.
    from tai42_skeleton.connectors.providers import registry as conn_registry

    manifest = Manifest.model_validate({"connectors": [_oauth_descriptor("iota").model_dump(mode="json")]})

    saved = dict(conn_registry._REGISTRY)
    conn_registry._REGISTRY.clear()

    async def run():
        async with app.app_context(manifest):
            assert conn_registry.get_provider("iota").id == "iota"
            assert "iota" in {p.id for p in conn_registry.list_providers()}

    try:
        asyncio.run(run())
    finally:
        conn_registry._REGISTRY.clear()
        conn_registry._REGISTRY.update(saved)


def test_reload_dropping_connector_unregisters_it():
    # The registration runs off the manifest each (re)load: a reload to a manifest
    # without the connector leaves the dropped provider unresolvable, never lingering.
    from tai42_skeleton.connectors.providers import registry as conn_registry

    with_conn = Manifest.model_validate({"connectors": [_oauth_descriptor("iota").model_dump(mode="json")]})
    empty = Manifest.model_validate({})

    saved = dict(conn_registry._REGISTRY)
    conn_registry._REGISTRY.clear()

    async def run():
        async with app.app_context(with_conn):
            assert conn_registry.get_provider("iota").id == "iota"
            await reload_with(app, empty)
            with pytest.raises(KeyError):
                conn_registry.get_provider("iota")

    try:
        asyncio.run(run())
    finally:
        conn_registry._REGISTRY.clear()
        conn_registry._REGISTRY.update(saved)


def test_duplicate_connector_ids_fail_boot():
    # The manifest validator rejects duplicate ids for a hand-written manifest;
    # bypass it (model_construct) to prove the boot-time registration loop is itself
    # a loud guard — a duplicate id across entries aborts boot, never a
    # quarantine-and-continue.
    from tai42_skeleton.connectors.providers import registry as conn_registry

    manifest = Manifest.model_construct(connectors=[_oauth_descriptor("iota"), _oauth_descriptor("iota")])

    saved = dict(conn_registry._REGISTRY)
    conn_registry._REGISTRY.clear()

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — boot aborts before the body runs

    try:
        with pytest.raises(ValueError, match="already registered"):
            asyncio.run(run())
    finally:
        conn_registry._REGISTRY.clear()
        conn_registry._REGISTRY.update(saved)


def test_manifest_rejects_duplicate_connector_ids_on_validate():
    # Hand-written manifest door: two connectors sharing an id are rejected loudly
    # at validation, naming the duplicate id.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="duplicate connector id 'iota'"):
        Manifest.model_validate(
            {
                "connectors": [
                    _oauth_descriptor("iota").model_dump(mode="json"),
                    _oauth_descriptor("iota").model_dump(mode="json"),
                ]
            }
        )


def test_start_logs_kind_summary_and_warns_once_on_noop_monitoring(monkeypatch, caplog):
    # A real boot must render the [kinds] summary and, with NoOp monitoring as the
    # active recorder, fire the once-per-process warning exactly once — the manual
    # smoke path made load-bearing. Reset the once-per-process guard and force the
    # monitoring registry back to its NoOp default so the boot sees "not configured".
    monkeypatch.setattr(ks, "_NOOP_WARNED", False)
    reset_monitoring()

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            pass

    with caplog.at_level(logging.INFO, logger=_BOOT_LOGGER):
        asyncio.run(run())

    messages = [r.getMessage() for r in caplog.records if r.name == _BOOT_LOGGER]
    assert "[kinds]" in messages
    assert any("monitoring: default" in m for m in messages)
    noop_warnings = [r for r in caplog.records if "monitoring: OFF" in r.getMessage()]
    assert len(noop_warnings) == 1


def test_start_fails_when_kind_status_collector_raises(monkeypatch):
    # A collector exception during the startup summary must fail the boot loudly,
    # never a silently degraded server with a missing table.
    def _boom():
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("tai42_skeleton.app.lifecycle.collect_kind_status", _boom)

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(RuntimeError, match="collector exploded"):
        asyncio.run(run())
