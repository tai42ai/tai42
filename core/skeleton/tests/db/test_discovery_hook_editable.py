"""Discovery of a chain-declaring plugin installed in an import-hook editable mode.

Such an install records none of its packages in installed metadata (see
:mod:`._hook_editable`), so its packages are found where the import system serves them and
attributed to the editable distribution whose PEP 610 source directory holds them. That is
enough for every door that starts from a module name: the contract-compat verdict, the
compat refusal (before any spec is read), the boot-time schema gate, and ``tai db migrate``
when the manifest loads a module of the plugin. ``tai db migrate`` enumerating what is
installed cannot list such a distribution's packages, so it names the distribution in its
output instead of dropping it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import tai42_kit.db.migrations as kit_migrations
from click.testing import CliRunner
from tai42_cli import app as app_module
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import (
    AppliedMigration,
    ComponentStatus,
    MigrationDiscoveryError,
    MigrationEntry,
    SchemaOutOfDateError,
    discover_migrations,
)
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.cli.native import db as db_cli
from tai42_skeleton.cli.native import doctor
from tai42_skeleton.config import ConfigManagerFactory
from tai42_skeleton.config.file_manager import FileConfigManager
from tai42_skeleton.db import boot_gate
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import (
    CompatVerdict,
    CorePluginBootError,
    distribution_map,
    module_compat,
    refuse_incompatible,
)

from . import _hook_editable
from ._hook_editable import DIST, TOOLS_MODULE, TOP_LEVEL

_COMPATIBLE = ">=0.1,<1000"
_INCOMPATIBLE = ">=999"


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A password alone configures the default database; no connection is opened while
    # entries are built. The manifest is read from its file, which exists only when a test
    # writes it.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setenv("TAI_MANIFEST_PATH", str(tmp_path / "absent-manifest.yml"))
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    monkeypatch.setattr(ConfigManagerFactory, "create", lambda: FileConfigManager(config_dir_path=str(tmp_path)))
    reset_all_settings()


@pytest.fixture(params=_hook_editable.SHAPES)
def shape(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(autouse=True)
def _forget_activated_modules() -> Iterator[None]:
    yield
    _hook_editable.forget_modules()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shape: str,
    *,
    spec_text: str | None = None,
    requires_contract: str | None = _COMPATIBLE,
) -> _hook_editable.HookInstall:
    """Lay the plugin out hook-served and process its site directory as ``site`` does."""
    install = _hook_editable.lay_out(
        tmp_path,
        shape,
        spec_text=_hook_editable.spec_yaml() if spec_text is None else spec_text,
        requires_contract=requires_contract,
    )
    _hook_editable.activate(monkeypatch, install)
    return install


@pytest.fixture
def hook_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str) -> _hook_editable.HookInstall:
    """The chain-declaring plugin, installed hook-served and compatible."""
    return _install(monkeypatch, tmp_path, shape)


def _chain_files(entry: MigrationEntry) -> list[str]:
    """The migration files in ``entry``'s directory, read through the Traversable the loader serves."""
    return [script.filename for script in discover_migrations(entry.migrations_dir)]


def _manifest_loading_the_plugin() -> Manifest:
    return Manifest.model_validate({"default_routers": "none", "tools": [{"title": "H", "module": TOOLS_MODULE}]})


def _persist_manifest_loading_the_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(f"default_routers: none\ntools:\n  - title: H\n    module: {TOOLS_MODULE}\n")
    monkeypatch.setenv("TAI_MANIFEST_PATH", str(path))
    reset_all_settings()


def test_compat_verdict_is_the_distributions_declared_range(hook_plugin: _hook_editable.HookInstall) -> None:
    assert module_compat(TOOLS_MODULE, distribution_map()) == CompatVerdict("compatible")
    assert TOP_LEVEL not in sys.modules


def test_incompatible_plugin_is_refused_before_its_spec_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str
) -> None:
    from tai42_skeleton.db.discovery import manifest_plugin_specs

    _install(monkeypatch, tmp_path, shape, spec_text="{ this is not a plugin spec", requires_contract=_INCOMPATIBLE)
    snapshot = distribution_map()

    with pytest.raises(CorePluginBootError, match=f"{DIST} requires tai42-contract"):
        refuse_incompatible(TOOLS_MODULE, "tools", snapshot)
    assert manifest_plugin_specs(_manifest_loading_the_plugin(), snapshot) == {}
    assert TOP_LEVEL not in sys.modules


async def test_boot_gate_checks_the_chain(
    monkeypatch: pytest.MonkeyPatch, hook_plugin: _hook_editable.HookInstall
) -> None:
    checked: list[MigrationEntry] = []

    async def status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        checked.extend(entries)
        return [
            ComponentStatus(
                component=entry.component,
                applied_versions=(),
                pending=tuple(discover_migrations(entry.migrations_dir)),
                mismatches=(),
            )
            for entry in entries
        ]

    monkeypatch.setattr(kit_migrations, "migration_status", status)
    monkeypatch.setattr(boot_gate, "component_store_configured", lambda component: component == DIST)
    monkeypatch.setattr(boot_gate, "component_store_settings", lambda component: SimpleNamespace(pg_dsn="runtime"))

    with pytest.raises(SchemaOutOfDateError, match=f"{DIST}: 1 pending \\({_hook_editable.CHAIN_FILE}\\)"):
        await boot_gate.assert_deployment_schemas_applied(_manifest_loading_the_plugin(), distribution_map())

    assert [_chain_files(entry) for entry in checked] == [[_hook_editable.CHAIN_FILE]]
    assert TOP_LEVEL not in sys.modules


def test_migrate_without_a_manifest_names_the_distribution(hook_plugin: _hook_editable.HookInstall) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    discovery = discover_plugin_chains()

    assert DIST not in {entry.component for entry in discovery.entries}
    assert [item.distribution for item in discovery.unlocatable] == [DIST]
    assert TOP_LEVEL not in sys.modules


def _fake_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the PG-touching runner calls: nothing to apply, every discovered chain up to date."""

    async def fake_apply(entries: list[MigrationEntry]) -> list[AppliedMigration]:
        return []

    async def fake_status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        return [
            ComponentStatus(component=entry.component, applied_versions=(1,), pending=(), mismatches=())
            for entry in entries
        ]

    monkeypatch.setattr(db_cli, "apply_migrations", fake_apply)
    monkeypatch.setattr(db_cli, "migration_status", fake_status)
    monkeypatch.setattr(doctor, "migration_status", fake_status)


@pytest.mark.parametrize("argv", [["db", "migrate"], ["db", "migrate", "--plan"], ["db", "status"]])
def test_db_command_without_a_manifest_prints_the_line_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, hook_plugin: _hook_editable.HookInstall, argv: list[str]
) -> None:
    _fake_runner(monkeypatch)

    result = CliRunner().invoke(app_module.app, argv)

    assert result.exit_code == 0, result.output
    assert f"distribution {DIST!r} is an editable install served by an import hook" in result.output


@pytest.mark.parametrize("argv", [["--json", "db", "migrate"], ["--json", "db", "status"]])
def test_db_command_json_lists_the_distribution(
    monkeypatch: pytest.MonkeyPatch, hook_plugin: _hook_editable.HookInstall, argv: list[str]
) -> None:
    _fake_runner(monkeypatch)

    result = CliRunner().invoke(app_module.app, argv)

    assert result.exit_code == 0, result.output
    assert [item["distribution"] for item in json.loads(result.output)["unlocatable"]] == [DIST]


def test_doctor_schema_probe_passes_and_names_the_distribution(
    monkeypatch: pytest.MonkeyPatch, hook_plugin: _hook_editable.HookInstall
) -> None:
    _fake_runner(monkeypatch)
    target = cast("PostgresConnectionSettings", SimpleNamespace(pg_host="db", pg_port=5432, pg_db="tai"))

    check = asyncio.run(doctor._probe_schema(target))

    assert check.status == doctor._OK, check.detail
    assert f"distribution {DIST!r} is an editable install served by an import hook" in check.detail


def test_migrate_with_a_manifest_loading_the_plugin_applies_its_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hook_plugin: _hook_editable.HookInstall
) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    _persist_manifest_loading_the_plugin(monkeypatch, tmp_path)

    discovery = discover_plugin_chains()

    entries = {entry.component: entry for entry in discovery.entries}
    assert DIST in entries, sorted(entries)
    assert _chain_files(entries[DIST]) == [_hook_editable.CHAIN_FILE]
    assert DIST not in {item.distribution for item in discovery.unlocatable}
    assert TOP_LEVEL not in sys.modules


def test_installer_entry_names_the_hook_served_install(hook_plugin: _hook_editable.HookInstall) -> None:
    from tai42_skeleton.db import installed_plugin_entry

    with pytest.raises(MigrationDiscoveryError, match=f"distribution {DIST!r} is an editable install served by"):
        installed_plugin_entry(DIST)


def test_hook_served_library_is_named_and_contributes_no_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str
) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    install = _hook_editable.lay_out(tmp_path, shape, spec_text=None, requires_contract=None)
    _hook_editable.activate(monkeypatch, install)

    discovery = discover_plugin_chains()

    assert DIST not in {entry.component for entry in discovery.entries}
    assert [item.distribution for item in discovery.unlocatable] == [DIST]
