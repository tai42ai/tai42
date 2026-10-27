"""The boot-time schema gate: refuse to build an epoch on an out-of-date database.

The refusal itself is kit's shared ``assert_chain_applied`` primitive (covered in kit's own
tests); these tests pin the skeleton's wiring around it — which chains the gate checks (the
skeleton and ``states`` chains, plus every chain a plugin the manifest loads declares), on
which connection (each component's RUNTIME store settings), with the exact ``tai db migrate``
remediation, and where it runs (``start()``, so every boot and every epoch build, before any
manifest module imports). Only kit's ``migration_status`` is faked, so the real primitive
renders the byte-exact refusal.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
import tai42_kit.db.migrations as kit_migrations
from tai42_kit.db import (
    ComponentStatus,
    MigrationDiscoveryError,
    MigrationEntry,
    MigrationScript,
    discover_migrations,
)
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.epoch import current_epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.db import boot_gate
from tai42_skeleton.db.boot_gate import SchemaOutOfDateError, assert_chain_applied
from tai42_skeleton.db.discovery import SKELETON_COMPONENT, SkippedChain, chain_skip_message
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.installed_packages import InstalledPackages
from tai42_skeleton.states.db import STATES_COMPONENT

from . import _chain_plugin as probe

_REMEDIATION = "Run 'tai db migrate' to apply the pending migrations, then restart."
_REFUSAL = "database schema is out of date — refusing to start: "
_PROBE_PENDING = f"{probe.COMPONENT}: 1 pending ({probe.CHAIN_FILE})"


def _script(version: int, name: str) -> MigrationScript:
    return MigrationScript(version=version, name=name, filename=f"{version:04d}_{name}.sql", checksum="c", sql="")


def _manifest_loading_probe() -> Manifest:
    return Manifest.model_validate(
        {"default_routers": "none", "tools": [{"title": "Chainprobe", "module": probe.TOOLS_MODULE}]}
    )


def _installed() -> InstalledPackages:
    from tai42_skeleton.marketplace.compat import distribution_map

    return distribution_map()


@pytest.fixture(autouse=True)
def _restore_process_env() -> Iterator[None]:
    """A successful build+swap leaves its applied env live in ``os.environ``; restore it."""
    from tai42_skeleton.app import applied_env

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = set()
    reset_all_settings()


@pytest.fixture
def isolated_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give the test its own ``sys.path`` list and drop every probe module afterwards."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    yield
    for name in [name for name in sys.modules if name.partition(".")[0] == probe.TOP_LEVEL]:
        del sys.modules[name]
    importlib.invalidate_caches()


def _install_probe(site: Path, *, spec_text: str | None = None, on_path: bool = True) -> None:
    probe.lay_out(site, spec_text=spec_text if spec_text is not None else probe.spec_yaml())
    if on_path:
        sys.path.insert(0, str(site))
    importlib.invalidate_caches()


@pytest.fixture
def chainprobe(tmp_path: Path, isolated_imports: None) -> Path:
    """The neutral chain-declaring distribution, installed on ``sys.path``."""
    site = tmp_path / "site"
    _install_probe(site)
    return site


def _configure(monkeypatch: pytest.MonkeyPatch, *, configured: set[str], binding_declared: bool = True) -> None:
    """Report ``configured`` components' stores as configured, each with a runtime-settings stand-in."""
    monkeypatch.setattr(boot_gate, "component_store_configured", lambda component: component in configured)
    monkeypatch.setattr(
        boot_gate, "component_store_settings", lambda component: SimpleNamespace(pg_dsn=f"runtime-{component}")
    )
    if binding_declared:
        monkeypatch.setenv(probe.BINDING_ENV, "default")
    else:
        monkeypatch.delenv(probe.BINDING_ENV, raising=False)
    reset_all_settings()


def _fake_status(
    monkeypatch: pytest.MonkeyPatch, *, pending: Mapping[str, Sequence[MigrationScript] | None] | None = None
) -> list[MigrationEntry]:
    """Fake kit's ``migration_status``: a component in ``pending`` has those scripts pending.

    ``None`` as a component's value means its whole packaged chain is pending. Returns the list
    the checked entries are recorded into.
    """
    checked: list[MigrationEntry] = []
    pending = dict(pending or {})

    async def status(entries: Sequence[MigrationEntry]) -> list[ComponentStatus]:
        checked.extend(entries)
        verdicts = []
        for entry in entries:
            scripts: Sequence[MigrationScript] = ()
            if entry.component in pending:
                listed = pending[entry.component]
                scripts = discover_migrations(entry.migrations_dir) if listed is None else listed
            verdicts.append(
                ComponentStatus(component=entry.component, applied_versions=(1,), pending=tuple(scripts), mismatches=())
            )
        return verdicts

    monkeypatch.setattr(kit_migrations, "migration_status", status)
    return checked


def test_gate_re_exports_the_kit_primitive() -> None:
    # Importers reaching for the gate's ``assert_chain_applied`` / ``SchemaOutOfDateError``
    # get the single kit definitions, not a skeleton-local duplicate.
    assert assert_chain_applied is kit_migrations.assert_chain_applied
    assert SchemaOutOfDateError is kit_migrations.SchemaOutOfDateError


# --- the platform chains ------------------------------------------------------


async def test_gate_checks_nothing_when_no_database_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, configured=set(), binding_declared=False)
    checked = _fake_status(monkeypatch)

    await boot_gate.assert_deployment_schemas_applied(Manifest.model_validate({}), _installed())

    assert checked == []


async def test_gate_verifies_the_platform_chains_on_their_runtime_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, configured={SKELETON_COMPONENT, STATES_COMPONENT})
    checked = _fake_status(monkeypatch)

    await boot_gate.assert_deployment_schemas_applied(Manifest.model_validate({}), _installed())

    assert [entry.component for entry in checked] == [SKELETON_COMPONENT, STATES_COMPONENT]
    # Verified on each component's runtime store, not its migrator identity.
    assert [entry.settings.pg_dsn for entry in checked] == ["runtime-skeleton", "runtime-states"]


async def test_skeleton_refusal_is_byte_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, configured={SKELETON_COMPONENT})
    _fake_status(monkeypatch, pending={SKELETON_COMPONENT: (_script(2, "add_thing"),)})

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await boot_gate.assert_deployment_schemas_applied(Manifest.model_validate({}), _installed())

    assert str(excinfo.value) == f"{_REFUSAL}skeleton: 1 pending (0002_add_thing.sql). {_REMEDIATION}"


# --- a loaded plugin's chain --------------------------------------------------


@pytest.mark.usefixtures("chainprobe")
def test_boot_refuses_a_pending_plugin_chain_before_importing_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    async def run() -> None:
        with pytest.raises(SchemaOutOfDateError) as excinfo:
            async with app.app_context(_manifest_loading_probe()):
                pass
        assert str(excinfo.value) == f"{_REFUSAL}{_PROBE_PENDING}. {_REMEDIATION}"

    asyncio.run(run())
    # The refusal comes before the component import: no module of the plugin ever ran.
    assert probe.TOOLS_MODULE not in sys.modules
    assert probe.TOP_LEVEL not in sys.modules


@pytest.mark.usefixtures("chainprobe")
def test_reload_refuses_a_pending_plugin_chain_and_the_live_epoch_keeps_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch, pending={probe.COMPONENT: None})
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: _manifest_loading_probe().model_dump())
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {probe.BINDING_ENV: "default"})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            live = current_epoch()
            manifest_before = app._manifest
            with pytest.raises(SchemaOutOfDateError) as excinfo:
                await reload_gate.run(app.admin.reload_config, reimports=True)
            assert str(excinfo.value) == f"{_REFUSAL}{_PROBE_PENDING}. {_REMEDIATION}"
            assert current_epoch() is live
            assert app._manifest is manifest_before

    asyncio.run(run())
    assert probe.TOOLS_MODULE not in sys.modules
    assert probe.TOP_LEVEL not in sys.modules


@pytest.mark.usefixtures("chainprobe")
async def test_one_refusal_names_every_stale_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, configured={SKELETON_COMPONENT, probe.COMPONENT})
    _fake_status(monkeypatch, pending={SKELETON_COMPONENT: (_script(2, "add_thing"),), probe.COMPONENT: None})

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert str(excinfo.value) == (
        f"{_REFUSAL}skeleton: 1 pending (0002_add_thing.sql) | {_PROBE_PENDING}. {_REMEDIATION}"
    )


@pytest.mark.usefixtures("chainprobe")
def test_applied_plugin_chain_boots_after_the_gate_checked_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _configure(monkeypatch, configured={SKELETON_COMPONENT, probe.COMPONENT})
    checked = _fake_status(monkeypatch)

    async def run() -> None:
        async with app.app_context(_manifest_loading_probe()):
            assert probe.TOOLS_MODULE in sys.modules

    asyncio.run(run())
    assert [entry.component for entry in checked] == [SKELETON_COMPONENT, probe.COMPONENT]
    plugin_entry = checked[1]
    assert Path(str(plugin_entry.migrations_dir)).name == "migrations"
    # The plugin chain is verified on its component's runtime store, like the platform chains.
    assert plugin_entry.settings.pg_dsn == f"runtime-{probe.COMPONENT}"


@pytest.mark.usefixtures("chainprobe")
async def test_undeclared_override_binding_is_a_logged_skip_not_a_gate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The feature whose store the override names is off: nothing to verify, and the skip is
    # the same visible line ``tai db migrate`` reports.
    _configure(monkeypatch, configured={probe.COMPONENT}, binding_declared=False)
    checked = _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    with caplog.at_level(logging.WARNING, logger=boot_gate.__name__):
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert checked == []
    assert chain_skip_message(SkippedChain(component=probe.COMPONENT)) in caplog.text


async def test_default_identity_with_an_unconfigured_store_is_a_logged_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None, caplog: pytest.LogCaptureFixture
) -> None:
    _install_probe(tmp_path / "site", spec_text=probe.spec_yaml(migrations_component=None))
    _configure(monkeypatch, configured=set(), binding_declared=False)
    checked = _fake_status(monkeypatch, pending={probe.DIST: None})

    with caplog.at_level(logging.INFO, logger=boot_gate.__name__):
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert checked == []
    assert f"the {probe.DIST!r} database is not configured — skipping its migration check" in caplog.text


async def test_default_identity_with_a_configured_store_is_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    _install_probe(tmp_path / "site", spec_text=probe.spec_yaml(migrations_component=None))
    _configure(monkeypatch, configured={probe.DIST}, binding_declared=False)
    _fake_status(monkeypatch, pending={probe.DIST: None})

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert str(excinfo.value) == f"{_REFUSAL}{probe.DIST}: 1 pending ({probe.CHAIN_FILE}). {_REMEDIATION}"


async def test_malformed_packaged_spec_refuses_naming_the_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    _install_probe(tmp_path / "site", spec_text="spec_version: 1\nname: chainprobe\n")
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch)

    with pytest.raises(MigrationDiscoveryError, match=probe.DIST):
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())


def test_prefix_only_distribution_is_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    # A plugin installed only into the plugins prefix becomes importable when boot puts the
    # prefix on ``sys.path``; the gate resolves its spec after that, so its chain is checked.
    import asyncio

    from tai42_skeleton.marketplace.prefix import prefix_site_dirs

    prefix = tmp_path / "prefix"
    _install_probe(Path(prefix_site_dirs(str(prefix))[0]), on_path=False)
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: str(prefix))
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    async def run() -> None:
        with pytest.raises(SchemaOutOfDateError) as excinfo:
            async with app.app_context(_manifest_loading_probe()):
                pass
        assert str(excinfo.value) == f"{_REFUSAL}{_PROBE_PENDING}. {_REMEDIATION}"

    asyncio.run(run())
    assert probe.TOOLS_MODULE not in sys.modules
    assert probe.TOP_LEVEL not in sys.modules


_RAISING_INIT = "raise AttributeError('contract drift')\n"


async def test_gate_resolves_a_plugin_chain_without_importing_the_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    # The chain's folder is resolved from where the packaged spec is installed, as
    # ``tai db migrate`` resolves it: the plugin's package never runs, so a package whose
    # import raises still gets the schema refusal.
    probe.lay_out(tmp_path / "site", spec_text=probe.spec_yaml(), init_body=_RAISING_INIT)
    sys.path.insert(0, str(tmp_path / "site"))
    importlib.invalidate_caches()
    _configure(monkeypatch, configured={probe.COMPONENT})
    checked = _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert str(excinfo.value) == f"{_REFUSAL}{_PROBE_PENDING}. {_REMEDIATION}"
    assert Path(str(checked[0].migrations_dir)) == tmp_path / "site" / probe.TOP_LEVEL / "migrations"
    assert probe.TOP_LEVEL not in sys.modules


def test_plugin_that_fails_to_import_aborts_boot_with_the_load_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    # With its chain applied, a plugin whose import raises reaches the component importer,
    # which aborts boot naming the module and the reason.
    import asyncio

    from tai42_skeleton.marketplace.compat import CorePluginBootError

    probe.lay_out(tmp_path / "site", spec_text=probe.spec_yaml(), init_body=_RAISING_INIT)
    sys.path.insert(0, str(tmp_path / "site"))
    importlib.invalidate_caches()
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch)

    async def run() -> None:
        with pytest.raises(CorePluginBootError, match=f"tools plugin '{probe.TOOLS_MODULE}' failed to import"):
            async with app.app_context(_manifest_loading_probe()):
                pass

    asyncio.run(run())


async def test_distribution_with_a_second_top_level_package_is_gated_as_migrate_applies_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    # A distribution installing two top-level packages, one of which packages the spec: the
    # gate checks the same chain folder ``tai db migrate`` applies.
    from tai42_skeleton.db import installed_plugin_entries

    probe.lay_out(tmp_path / "site", spec_text=probe.spec_yaml(), second_top_level="tai42_chainprobe_extras")
    sys.path.insert(0, str(tmp_path / "site"))
    importlib.invalidate_caches()
    _configure(monkeypatch, configured={probe.COMPONENT})
    checked = _fake_status(monkeypatch, pending={probe.COMPONENT: None})
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    reset_all_settings()

    applied = {entry.component: entry for entry in installed_plugin_entries()}
    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert str(excinfo.value) == f"{_REFUSAL}{_PROBE_PENDING}. {_REMEDIATION}"
    assert [entry.migrations_dir for entry in checked] == [applied[probe.COMPONENT].migrations_dir]


# A plugin built against a newer contract: its metadata declares a contract range the running
# one is outside of, and its packaged spec carries a key this contract does not know.
_NEWER_CONTRACT_SPEC = probe.spec_yaml() + "future_field: enabled\n"
_UNSATISFIED_RANGE = "<0.0.1"
_COMPAT_REFUSAL = (
    f"tools plugin '{probe.TOOLS_MODULE}' is incompatible and cannot load: "
    f"{probe.DIST} requires tai42-contract {_UNSATISFIED_RANGE}"
)
# A spec that is malformed for the contract the plugin itself declares it supports.
_MALFORMED_SPEC = "spec_version: 1\nname: chainprobe\n"


def _install_newer_contract_probe(site: Path) -> None:
    probe.lay_out(site, spec_text=_NEWER_CONTRACT_SPEC, requires_contract=_UNSATISFIED_RANGE)
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()


def _surfaced_reload_failure(exc: BaseException) -> dict[str, object]:
    """The body a fleet reload door answers when its local rebuild raised ``exc``."""
    import asyncio
    import json

    from starlette.requests import Request

    from tai42_skeleton.app import error_envelope
    from tai42_skeleton.app.bus.models import FleetResult
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    wrapped = FleetBroadcastError("reload_config", FleetResult(op="reload_config", reachable=True), exc)
    wrapped.__cause__ = exc
    scope = {"type": "http", "method": "POST", "path": "/api/manifest/replace", "query_string": b"", "headers": []}
    response = asyncio.run(error_envelope.fleet_broadcast_error_handler(Request(scope), wrapped))
    return json.loads(bytes(response.body))


def _reload_into_probe_manifest(monkeypatch: pytest.MonkeyPatch) -> BaseException:
    """Reload a live empty deployment into the manifest loading the probe; return what the reload raised."""
    import asyncio

    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: _manifest_loading_probe().model_dump())
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {probe.BINDING_ENV: "default"})
    raised: list[BaseException] = []

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            live = current_epoch()
            try:
                await reload_gate.run(app.admin.reload_config, reimports=True)
            except Exception as exc:
                raised.append(exc)
            assert current_epoch() is live

    asyncio.run(run())
    assert raised, "the reload applied"
    return raised[0]


async def test_gate_leaves_a_contract_incompatible_plugin_to_its_compat_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    # A plugin the compat check refuses never loads, so the gate does not read its spec.
    _install_newer_contract_probe(tmp_path / "site")
    _configure(monkeypatch, configured={probe.COMPONENT})
    checked = _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    await boot_gate.assert_deployment_schemas_applied(_manifest_loading_probe(), _installed())

    assert [entry.component for entry in checked] == []


def test_boot_refuses_a_contract_incompatible_plugin_with_its_compat_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    import asyncio

    from tai42_skeleton.marketplace.compat import CorePluginBootError

    _install_newer_contract_probe(tmp_path / "site")
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    async def run() -> None:
        with pytest.raises(CorePluginBootError) as excinfo:
            async with app.app_context(_manifest_loading_probe()):
                pass
        assert str(excinfo.value).startswith(_COMPAT_REFUSAL)

    asyncio.run(run())
    assert probe.TOP_LEVEL not in sys.modules


def test_reload_refuses_a_contract_incompatible_plugin_with_its_compat_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    from tai42_skeleton.marketplace.compat import CorePluginBootError

    _install_newer_contract_probe(tmp_path / "site")
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch, pending={probe.COMPONENT: None})

    raised = _reload_into_probe_manifest(monkeypatch)

    assert isinstance(raised, CorePluginBootError)
    assert str(_surfaced_reload_failure(raised)["error"]).startswith(_COMPAT_REFUSAL)
    assert probe.TOP_LEVEL not in sys.modules


def test_boot_refuses_a_malformed_packaged_spec_naming_the_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    import asyncio

    _install_probe(tmp_path / "site", spec_text=_MALFORMED_SPEC)
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch)

    async def run() -> None:
        with pytest.raises(
            MigrationDiscoveryError, match=f"invalid plugin spec packaged in distribution '{probe.DIST}'"
        ):
            async with app.app_context(_manifest_loading_probe()):
                pass

    asyncio.run(run())
    assert probe.TOP_LEVEL not in sys.modules


def test_reload_surfaces_a_malformed_packaged_spec_naming_the_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_imports: None
) -> None:
    _install_probe(tmp_path / "site", spec_text=_MALFORMED_SPEC)
    _configure(monkeypatch, configured={probe.COMPONENT})
    _fake_status(monkeypatch)

    raised = _reload_into_probe_manifest(monkeypatch)

    assert isinstance(raised, MigrationDiscoveryError)
    surfaced = _surfaced_reload_failure(raised)
    assert surfaced["error"] == str(raised)
    assert f"invalid plugin spec packaged in distribution '{probe.DIST}'" in str(surfaced["error"])
    assert probe.TOP_LEVEL not in sys.modules
