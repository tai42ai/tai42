"""Manifest module import into the serving surface: additive quarantine-and-continue,
scalar-slot aborts, the auth-provider quarantine gate, extensions/tools validation, and
the ``_initialize_components`` MCP-load seam."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from tai42_skeleton.app import lifecycle as lifecycle_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import compat as mkt_compat
from tai42_skeleton.marketplace.compat import CompatVerdict, CorePluginBootError
from tai42_skeleton.plugins.quarantine import quarantined_plugins
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


def test_webhook_verifier_modules_import_failure_quarantines():
    # A broken additive module is quarantined loudly (recorded name → reason) and
    # the server boots without it, serving everything else.
    manifest = Manifest.model_validate({"webhook_verifier_modules": ["totally_bogus_verifier_pkg"]})

    async def run():
        async with app.app_context(manifest):
            reason = quarantined_plugins()["totally_bogus_verifier_pkg"]
            assert "failed to import" in reason

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


def test_channel_modules_import_failure_quarantines():
    # A broken channel module quarantines like a broken verifier module: the
    # boot proceeds, the channel is absent, and the quarantine entry (not a
    # silent skip) is what says why.
    manifest = Manifest.model_validate({"channel_modules": ["totally_bogus_channel_pkg"]})

    async def run():
        async with app.app_context(manifest):
            assert "failed to import" in quarantined_plugins()["totally_bogus_channel_pkg"]
            with pytest.raises(KeyError, match="unknown channel"):
                app.channels.get("fixture_channel")

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


def test_start_quarantines_broken_additive_module_and_boots():
    # An ADDITIVE manifest module that fails to import quarantines (loud entry,
    # boot proceeds); each start()/reload pass owns a FRESH quarantine
    # generation, so dropping the broken module clears its entry.
    broken = Manifest.model_validate({"lifecycle_modules": ["totally_bogus_pkg_xyz"]})
    clean = Manifest.model_validate({})

    async def run():
        async with app.app_context(broken):
            assert "failed to import" in quarantined_plugins()["totally_bogus_pkg_xyz"]
            await reload_with(app, clean)
            assert quarantined_plugins() == {}

    asyncio.run(run())


def test_start_aborts_on_broken_scalar_slot_module():
    # A SCALAR slot (backend/storage/monitoring) never quarantines: the server
    # cannot run without it, so start() aborts with the typed error naming the
    # slot and the module.
    manifest = Manifest.model_validate({"monitoring_module": "totally_bogus_pkg_xyz"})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.raises(CorePluginBootError, match="monitoring_module plugin 'totally_bogus_pkg_xyz'"):
        asyncio.run(run())


def test_start_skips_incompatible_additive_module_without_importing():
    # An incompatible verdict quarantines BEFORE the import: the module's code
    # never runs (importing it is what misbehaves), and its quarantine reason
    # carries the verdict text.
    manifest = Manifest.model_validate({"lifecycle_modules": ["tests.app._fixtures.lifecycle_reg"]})
    real_import = lifecycle_module.import_or_reload_package

    def _refuse(module, dist_map=None):
        if module == "tests.app._fixtures.lifecycle_reg":
            return CompatVerdict("incompatible", "needs tai42-contract <0.2, but 0.3.0 is running")
        return CompatVerdict("unknown", "no dist")

    imported: list[str] = []

    def _spy_import(module):
        imported.append(module)
        return real_import(module)

    async def run():
        async with app.app_context(manifest):
            assert "needs tai42-contract <0.2" in quarantined_plugins()["tests.app._fixtures.lifecycle_reg"]
            assert "tests.app._fixtures.lifecycle_reg" not in imported

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _refuse)
        mp.setattr(lifecycle_module, "import_or_reload_package", _spy_import)
        asyncio.run(run())


def test_start_aborts_on_incompatible_scalar_slot_without_importing():
    # An incompatible SCALAR slot aborts boot BEFORE the import (importing an
    # incompatible plugin is exactly what misbehaves) with the typed error naming
    # the slot and the verdict — never a quarantine-and-continue.
    manifest = Manifest.model_validate({"monitoring_module": "totally_bogus_pkg_xyz"})
    real_import = lifecycle_module.import_or_reload_package

    def _refuse(module, dist_map=None):
        if module == "totally_bogus_pkg_xyz":
            return CompatVerdict("incompatible", "needs tai42-contract <0.2, but 0.3.0 is running")
        return CompatVerdict("unknown", "no dist")

    imported: list[str] = []

    def _spy_import(module):
        imported.append(module)
        return real_import(module)

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() fails before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _refuse)
        mp.setattr(lifecycle_module, "import_or_reload_package", _spy_import)
        with pytest.raises(CorePluginBootError, match=r"cannot boot: needs tai42-contract <0\.2"):
            asyncio.run(run())
    assert "totally_bogus_pkg_xyz" not in imported


def _quarantine_module_compat(target: str):
    """A ``module_compat`` stub that rules ``target`` incompatible (a versioned
    reason) and everything else unknown — so ``target`` quarantines on the additive
    import pass without importing."""

    def _refuse(module, dist_map=None):
        if module == target:
            return CompatVerdict("incompatible", "needs tai42-contract <0.2, but 0.3.0 is running")
        return CompatVerdict("unknown", "no dist")

    return _refuse


def test_quarantined_identity_provider_aborts_boot_naming_it():
    # A configured identity provider whose lifecycle module quarantined must ABORT
    # boot (not quarantine-and-continue): the typed error names the module, the
    # versioned quarantine reason, and the provider that failed to register.

    from tai42_skeleton.access_control import settings as ac_settings

    provider_module = "tests.app._fixtures.lifecycle_reg"
    manifest = Manifest.model_validate({"lifecycle_modules": [provider_module]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _quarantine_module_compat(provider_module))
        mp.setattr(
            ac_settings,
            "access_control_settings",
            lambda: ac_settings.AccessControlSettings(enable=True, auth_providers=["quarantined-idp"]),
        )
        with pytest.raises(
            CorePluginBootError,
            match=r"quarantined-idp.*tests\.app\._fixtures\.lifecycle_reg.*needs tai42-contract <0\.2",
        ):
            asyncio.run(run())


def test_quarantined_accounts_provider_aborts_boot_naming_it():
    # An accounts provider registers into the identity registry too, so a configured
    # accounts provider whose lifecycle module quarantined is caught the same way —
    # the boot aborts rather than serving password/bootstrap login dead.

    from tai42_skeleton.access_control import settings as ac_settings

    provider_module = "tests.app._fixtures.lifecycle_reg"
    manifest = Manifest.model_validate({"lifecycle_modules": [provider_module]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _quarantine_module_compat(provider_module))
        mp.setattr(
            ac_settings,
            "access_control_settings",
            lambda: ac_settings.AccessControlSettings(enable=True, auth_providers=["accounts-postgres"]),
        )
        with pytest.raises(
            CorePluginBootError,
            match=r"accounts-postgres.*tests\.app\._fixtures\.lifecycle_reg.*needs tai42-contract <0\.2",
        ):
            asyncio.run(run())


def test_unresolved_provider_and_unrelated_quarantine_reports_both_without_blame():
    # Misattribution guard: an operator typo (a provider name nothing provides)
    # plus an UNRELATED non-auth lifecycle module quarantining trip the same gate.
    # The abort must state the two facts as SEPARATE enumerations — the unresolved
    # provider name AND the quarantined module + reason — and must NOT assert the
    # innocent module is the missing provider's cause.

    from tai42_skeleton.access_control import settings as ac_settings

    # lifecycle_reg registers only startup/shutdown handlers + a middleware (no
    # identity provider), so it is genuinely unrelated to the missing provider.
    unrelated_module = "tests.app._fixtures.lifecycle_reg"
    manifest = Manifest.model_validate({"lifecycle_modules": [unrelated_module]})

    async def run():
        async with app.app_context(manifest):
            pass  # pragma: no cover — start() aborts before the body runs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _quarantine_module_compat(unrelated_module))
        mp.setattr(
            ac_settings,
            "access_control_settings",
            lambda: ac_settings.AccessControlSettings(enable=True, auth_providers=["redis_api_key_provdr"]),
        )
        with pytest.raises(CorePluginBootError) as excinfo:
            asyncio.run(run())

    message = str(excinfo.value)
    # Both facts present, each stated distinctly.
    assert "redis_api_key_provdr" in message
    assert unrelated_module in message
    assert "needs tai42-contract <0.2" in message
    # No causal phrasing pinning the missing provider on the quarantined module.
    assert "their provider module" not in message
    assert "because" not in message


def test_no_providers_configured_boots_with_a_quarantined_provider():
    # An intentionally auth-less deployment (gate off) requires no provider, so a
    # quarantined provider module degrades nothing — boot proceeds exactly as today.
    from types import SimpleNamespace

    from tai42_skeleton.access_control import settings as ac_settings

    provider_module = "tests.app._fixtures.lifecycle_reg"
    manifest = Manifest.model_validate({"lifecycle_modules": [provider_module]})
    booted = False

    async def run():
        nonlocal booted
        async with app.app_context(manifest):
            booted = True

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _quarantine_module_compat(provider_module))
        mp.setattr(
            ac_settings,
            "access_control_settings",
            lambda: SimpleNamespace(enable=False, auth_providers=[], reserved_public_pin_prefixes=("/api/auth",)),
        )
        asyncio.run(run())

    assert booted


def test_quarantined_non_auth_lifecycle_module_still_boots():
    # A quarantined NON-auth lifecycle module leaves the configured providers
    # resolving (redis registered), so the auth-slot abort does not fire: the boot
    # proceeds and the module is quarantined, not fatal — existing behavior intact.
    provider_module = "tests.app._fixtures.lifecycle_reg"
    manifest = Manifest.model_validate({"lifecycle_modules": [provider_module]})

    async def run():
        async with app.app_context(manifest):
            assert provider_module in quarantined_plugins()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mkt_compat, "module_compat", _quarantine_module_compat(provider_module))
        asyncio.run(run())


def test_start_quarantines_broken_extensions_module_and_boots():
    # A broken extensions module quarantines like any other additive plugin; with
    # no tool requesting its extensions, extension validation still passes.
    manifest = Manifest.model_validate({"extensions_modules": ["totally_bogus_ext_pkg"]})

    async def run():
        async with app.app_context(manifest):
            assert "failed to import" in quarantined_plugins()["totally_bogus_ext_pkg"]

    asyncio.run(run())


def test_missing_extensions_attributed_to_quarantined_extensions_module(caplog):
    # A tool requests an extension the quarantined extensions module would have
    # registered: the validation failure is attributed to the quarantine (one
    # loud error log, boot proceeds) instead of aborting the boot the quarantine
    # just saved.
    manifest = Manifest.model_validate(
        {
            "extensions_modules": ["totally_bogus_ext_pkg"],
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
            assert "failed to import" in quarantined_plugins()["totally_bogus_ext_pkg"]

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())
    assert any("extension validation failed" in record.message for record in caplog.records)


def test_missing_extensions_without_a_quarantined_extensions_module_still_abort():
    # The attribution is scoped to a quarantine: with every extensions module
    # imported, a missing extension is genuine manifest misconfiguration and
    # stays a loud abort.
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


def test_start_ignores_missing_tools_of_quarantined_tools_module():
    # A quarantined tools module's included tool names are legitimately absent:
    # tool validation must not abort the boot the quarantine just saved, while a
    # HEALTHY module's tools still load.
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
            assert "failed to import" in quarantined_plugins()["totally_bogus_tools_pkg"]
            tools = await app.tools.get_tools()
            assert "greet" in tools
            assert "phantom_tool" not in tools

    asyncio.run(run())
