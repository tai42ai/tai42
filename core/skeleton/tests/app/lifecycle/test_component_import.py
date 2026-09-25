"""Manifest module import into the serving surface: the shared boot-abort seam for every
manifest-declared module (additive roles and scalar slots), extensions/tools validation, and
the ``_initialize_components`` MCP-load seam."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tai42_skeleton.app import lifecycle as lifecycle_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import compat as mkt_compat
from tai42_skeleton.marketplace.compat import CompatVerdict, CorePluginBootError
from tai42_skeleton.tools import mcp_health

from .._fixtures.reload import reload_with
from ._doubles import _cfg, _FakeMcpTool, _Mixin


def test_webhook_verifier_modules_import_registers_verifier():
    # A manifest ``webhook_verifier_modules`` entry is imported at app load like
    # the lifecycle modules; its import-time register(...) side-effect lands in
    # the verifier registry, and a reload (which resets the registry, then
    # re-imports) re-registers cleanly rather than tripping the duplicate guard.
    manifest = Manifest.model_validate({"webhook_verifier_modules": ["tests.app._fixtures.webhook_verifier_mod"]})

    async def run():
        async with app.app_context(manifest):
            assert app.webhook_verifiers.get("fixture_verifier") is not None
            await reload_with(app, manifest)
            assert app.webhook_verifiers.get("fixture_verifier") is not None

    asyncio.run(run())


def test_webhook_verifier_modules_import_failure_aborts_boot():
    # A manifest-declared verifier module that fails to import aborts boot through the
    # shared abort seam, naming the module, its kind and the reason.
    manifest = Manifest.model_validate({"webhook_verifier_modules": ["totally_bogus_verifier_pkg"]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(
        CorePluginBootError, match="webhook_verifier plugin 'totally_bogus_verifier_pkg' failed to import"
    ):
        asyncio.run(run())


def test_channel_modules_import_registers_channel():
    # A manifest ``channel_modules`` entry is imported at app load like the
    # verifier modules; its import-time register(...) side-effect lands in the
    # channel registry, and a reload (which resets the registry, then
    # re-imports) re-registers cleanly rather than tripping the duplicate guard.
    manifest = Manifest.model_validate({"channel_modules": ["tests.app._fixtures.channel_mod"]})

    async def run():
        async with app.app_context(manifest):
            assert app.channels.get("fixture_channel") is not None
            await reload_with(app, manifest)
            assert app.channels.get("fixture_channel") is not None

    asyncio.run(run())


def test_channel_modules_import_failure_aborts_boot():
    # A manifest-declared channel module that fails to import aborts boot, naming the
    # module, its kind and the reason — the server never comes up missing a channel.
    manifest = Manifest.model_validate({"channel_modules": ["totally_bogus_channel_pkg"]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(CorePluginBootError, match="channel plugin 'totally_bogus_channel_pkg' failed to import"):
        asyncio.run(run())


def test_reload_dropping_channel_module_unregisters_channel():
    # The per-start reset is real: a reload to a manifest without the channel
    # module leaves the dropped channel unresolvable, never lingering.
    with_channel = Manifest.model_validate({"channel_modules": ["tests.app._fixtures.channel_mod"]})
    empty = Manifest.model_validate({})

    async def run():
        async with app.app_context(with_channel):
            assert app.channels.get("fixture_channel") is not None
            await reload_with(app, empty)
            with pytest.raises(KeyError, match="unknown channel"):
                app.channels.get("fixture_channel")

    asyncio.run(run())


def test_reload_dropping_prompt_module_removes_its_prompts():
    # Reload removes stale prompts symmetrically with tools: a reload dropping the
    # prompt-owning module leaves NONE of its prompts live.
    with_prompt = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.prompt_mod"]})
    empty = Manifest.model_validate({})

    async def run():
        async with app.app_context(with_prompt):
            names = {p.name for p in await app._fast_mcp.list_prompts()}
            assert "fixture_prompt" in names
            await reload_with(app, empty)
            names = {p.name for p in await app._fast_mcp.list_prompts()}
            assert "fixture_prompt" not in names

    asyncio.run(run())


def test_initialize_components_loads_and_records_failed_mcp(monkeypatch):
    # A manifest MCP whose probe times out is skipped + recorded, not fatal.
    manifest = Manifest.model_validate({"mcp": [_cfg("downsvc").model_dump()]})
    monkeypatch.setattr(app, "_probe_mcp", AsyncMock(side_effect=TimeoutError("slow")))

    async def run():
        async with app.app_context(manifest):
            assert app.admin.list_failed_mcps() == [{"title": "downsvc", "status": "unavailable"}]

    asyncio.run(run())


def test_initialize_components_binds_probed_mcp_tools(monkeypatch):
    manifest = Manifest.model_validate({"mcp": [_cfg("upsvc").model_dump()]})
    monkeypatch.setattr(app, "_probe_mcp", AsyncMock(return_value=[_FakeMcpTool()]))

    async def run():
        async with app.app_context(manifest):
            status = app.admin.live_mcp_status()
            assert "upsvc" in status["bound"]

    asyncio.run(run())


def test_initialize_components_prunes_health_to_configured_titles(monkeypatch):
    """A fresh epoch's MCP load prunes the health store to the CONFIGURED titles: a
    title dropped from the manifest loses its history, while a configured title keeps
    it — even when that title probed unavailable this build."""
    mcp_health._HEALTH.clear()
    manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    # "svc" probes unavailable this build (lands in failures), so its history must
    # survive; "gone" is not in config and must be pruned.
    monkeypatch.setattr(app, "_probe_mcp", AsyncMock(side_effect=TimeoutError("slow")))
    mcp_health.record_success("svc")
    mcp_health.record_failure("gone", RuntimeError("was removed"))

    async def run():
        async with app.app_context(manifest):
            assert "svc" in mcp_health._HEALTH
            assert "gone" not in mcp_health._HEALTH

    try:
        asyncio.run(run())
    finally:
        mcp_health._HEALTH.clear()


def test_initialize_helpers_require_started():
    m = _Mixin()  # _manifest is None
    with pytest.raises(RuntimeError, match="not started"):
        m._initialize_registries()
    with pytest.raises(RuntimeError, match="not started"):
        m._initialize_components()


def test_start_imports_lifecycle_router_and_middleware_modules():
    # The lifecycle/routers/middlewares module loops each import their listed
    # packages (pointed at a neutral fixture package here).
    manifest = Manifest.model_validate(
        {
            "lifecycle_modules": ["tests.app._fixtures.neutral"],
            "routers_modules": ["tests.app._fixtures.neutral"],
            "middlewares_modules": ["tests.app._fixtures.neutral"],
            # "none" keeps this focused on the loop importing the listed module,
            # not the default set.
            "default_routers": "none",
        }
    )

    async def run():
        async with app.app_context(manifest):
            assert app._manifest is manifest

    asyncio.run(run())


def test_start_aborts_on_broken_additive_module():
    # An additive manifest module that fails to import aborts boot through the shared
    # abort seam, naming the module, its kind and the reason — the operator declared
    # it, so it cannot load is corrupt configuration, not a degradation to serve around.
    manifest = Manifest.model_validate({"lifecycle_modules": ["totally_bogus_pkg_xyz"]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(CorePluginBootError, match="lifecycle plugin 'totally_bogus_pkg_xyz' failed to import"):
        asyncio.run(run())


def test_start_aborts_on_broken_scalar_slot_module():
    # A scalar slot (backend/storage/monitoring) aborts boot on the same seam: the
    # server cannot run without it, and the typed error names the slot and the module.
    manifest = Manifest.model_validate({"monitoring_module": "totally_bogus_pkg_xyz"})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(CorePluginBootError, match="monitoring plugin 'totally_bogus_pkg_xyz' failed to import"):
        asyncio.run(run())


def _incompatible_module_compat(target: str):
    """A ``module_compat`` stub that rules ``target`` incompatible (a versioned reason)
    and everything else unknown — so ``target`` aborts the import pass before importing."""

    def _refuse(module, dist_map=None):
        if module == target:
            return CompatVerdict("incompatible", "needs tai42-contract <0.2, but 0.3.0 is running")
        return CompatVerdict("unknown", "no dist")

    return _refuse


def test_start_aborts_on_incompatible_additive_module_without_importing():
    # An incompatible verdict aborts boot BEFORE the import (importing an incompatible
    # plugin is exactly what misbehaves): the module's code never runs, and the typed
    # error carries the verdict text.
    manifest = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.lifecycle_reg"]})
    real_import = lifecycle_module.import_or_reload_package

    imported: list[str] = []

    def _spy_import(module, *args, **kwargs):
        imported.append(module)
        return real_import(module, *args, **kwargs)

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _incompatible_module_compat("tests.app._fixtures.lifecycle_reg"))
        mp.setattr(lifecycle_module, "import_or_reload_package", _spy_import)
        with pytest.raises(
            CorePluginBootError,
            match=r"lifecycle plugin .* is incompatible.*needs tai42-contract <0\.2",
        ):
            asyncio.run(run())
    assert "tests.app._fixtures.lifecycle_reg" not in imported


def test_start_aborts_on_incompatible_scalar_slot_without_importing():
    # An incompatible scalar slot aborts boot BEFORE the import, on the same seam, with
    # the typed error naming the slot and the verdict.
    manifest = Manifest.model_validate({"monitoring_module": "totally_bogus_pkg_xyz"})
    real_import = lifecycle_module.import_or_reload_package

    imported: list[str] = []

    def _spy_import(module, *args, **kwargs):
        imported.append(module)
        return real_import(module, *args, **kwargs)

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _incompatible_module_compat("totally_bogus_pkg_xyz"))
        mp.setattr(lifecycle_module, "import_or_reload_package", _spy_import)
        with pytest.raises(
            CorePluginBootError,
            match=r"monitoring plugin 'totally_bogus_pkg_xyz' is incompatible.*needs tai42-contract <0\.2",
        ):
            asyncio.run(run())
    assert "totally_bogus_pkg_xyz" not in imported


def test_start_aborts_on_broken_extensions_module():
    # A manifest extensions module that fails to import aborts boot before extension
    # validation runs, naming the module, its kind and the reason.
    manifest = Manifest.model_validate({"extensions_modules": ["totally_bogus_ext_pkg"]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(CorePluginBootError, match="extensions plugin 'totally_bogus_ext_pkg' failed to import"):
        asyncio.run(run())


def test_missing_requested_extension_aborts_boot():
    # With every extensions module imported, a tool requesting an extension no module
    # registers is genuine manifest misconfiguration: extension validation aborts loudly.
    manifest = Manifest.model_validate(
        {
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_a",
                    "include": ["greet"],
                    "extensions": {"greet": [["phantom_ext"]]},
                }
            ],
        }
    )

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(TaiValidationError, match="phantom_ext"):
        asyncio.run(run())


def test_start_aborts_on_broken_tools_module():
    # A manifest tools module that fails to import aborts boot, naming the module, its
    # kind and the reason — its declared tools never load, and neither does the server.
    manifest = Manifest.model_validate(
        {
            "tools": [
                {"title": "fxt", "module": "tests.app._fixtures.tools_a", "include": ["greet"]},
                {"title": "broken", "module": "totally_bogus_tools_pkg", "include": ["phantom_tool"]},
            ]
        }
    )

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(CorePluginBootError, match="tools plugin 'totally_bogus_tools_pkg' failed to import"):
        asyncio.run(run())


def test_cross_owner_route_collision_keeps_its_type_through_the_seam():
    # A cross-owner route collision raised while a module registers is a resolvable DOMAIN
    # condition — the operator remaps the item's base and the marketplace remount reload
    # applies it — not an import/compat failure. The seam lets it keep its own type so those
    # callers can act on it; at boot, with no remount to follow, it still propagates and
    # aborts boot. Every OTHER import-time exception is wrapped as CorePluginBootError.
    from tai42_skeleton.app.route_registry import CrossOwnerRouteCollisionError

    manifest = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.neutral"]})

    def _collide(module, *args, **kwargs):
        raise CrossOwnerRouteCollisionError(
            "route GET /api/x (owner a) collides with GET /api/x (owner b) — "
            "one owner per route shape; remap the mount base to resolve"
        )

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lifecycle_module, "import_or_reload_package", _collide)
        with pytest.raises(CrossOwnerRouteCollisionError, match="remap the mount base to resolve"):
            asyncio.run(run())


def test_non_collision_registration_error_is_wrapped_as_boot_abort():
    # The contrast to the route-collision passthrough: any OTHER exception raised while a
    # manifest module imports/registers is a module that cannot load, so it is wrapped as
    # CorePluginBootError naming the module and its kind — the boot-abort seam.
    manifest = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.neutral"]})

    def _boom(module, *args, **kwargs):
        raise RuntimeError("registration blew up")

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lifecycle_module, "import_or_reload_package", _boom)
        with pytest.raises(CorePluginBootError, match=r"lifecycle plugin .* failed to import: .*registration blew up"):
            asyncio.run(run())
