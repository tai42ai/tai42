"""The installer's plugin-migration step: run the plugin's declared schema
chain AFTER the package lands and BEFORE the manifest patch activates it.

Fully faked seams (reusing the installer harness): the kit runner
(``apply_migrations``) and the entry resolver (``installed_plugin_entry``) are
patched so the tests pin the WIRING — the migrate call's ordering relative to the
manifest write, the opt-in skip for a plugin with no chain, and the fail-loud /
no-partial-state guarantee when a migration fails. An ``mcp-server`` package's chain
is resolved for real, from the server environment the harness pip lays it into.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest
import yaml
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import AppliedMigration, ChecksumMismatchError, MigrationEntry

from tai42_skeleton.marketplace import installer_base as installer_base_module
from tai42_skeleton.marketplace.errors import PluginMigrationError
from tai42_skeleton.operations._marketplace_errors import to_operation_error

from ._specs import make_resolved, make_spec, mcp_server_item, tool_item
from .test_installer import Harness, layer_inputs


def _spec_with_migrations(*, name: str = "acct", package: str = "tai42-acct", version: str = "1.0.0") -> PluginSpec:
    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": name,
            "package": package,
            "version": version,
            "description": "A table-owning test plugin",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1.0",
            "categories": ["dev"],
            "provides": [tool_item(module="tai42_acct.tools.x", name="acct-x")],
            "migrations": "migrations",
        }
    )


# A server package name no environment of the test process holds.
_SERVER_PACKAGE = "tai42-srvchain"


def _server_spec_with_migrations(*, version: str = "1.0.0") -> PluginSpec:
    """An ``mcp-server`` package declaring a chain: its one item launches ``srv-mcp``, it declares no contract."""
    return make_spec(
        name="srvchain", package=_SERVER_PACKAGE, version=version, provides=[mcp_server_item()], contract=None
    ).model_copy(update={"migrations": "migrations"})


def _lay_server_spec_and_chain(h: Harness, spec: PluginSpec, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the harness pip lay ``spec`` and a one-file chain into the server venv, and name the plugins dir."""
    assert spec.package is not None
    h.pip.console_scripts[spec.package] = ["srv-mcp"]
    h.pip.server_files[spec.package] = {
        "tai42_srvchain/tai-plugin.yml": yaml.safe_dump(spec.model_dump(mode="json", exclude_none=True)),
        "tai42_srvchain/migrations/0001_baseline.sql": "CREATE TABLE srvchain_marks (id integer PRIMARY KEY);\n",
    }
    monkeypatch.setattr(
        "tai42_skeleton.marketplace.environments.current_builds.configured_plugins_dir", lambda: h.plugins_dir
    )
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")


def _patch_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")


async def test_install_runs_migrations_before_manifest_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    spec = _spec_with_migrations()
    h.registry.resolved = make_resolved(spec, version="1.0.0")

    entries_seen: list[Any] = []

    async def _fake_apply(entries: Any) -> list[AppliedMigration]:
        h.events.append("migrate")
        entries_seen.append(entries)
        return [AppliedMigration("tai42-acct", 1, "baseline", "abc")]

    monkeypatch.setattr(installer_base_module, "apply_migrations", _fake_apply)
    monkeypatch.setattr(installer_base_module, "installed_plugin_entry", lambda distribution: object())

    await h.installer().install("tai42/acct")

    order = [e for e in h.events if e in ("pip", "migrate", "cm:write", "store:record")]
    assert order == ["pip", "migrate", "cm:write", "store:record"]
    assert len(entries_seen) == 1


async def test_plugin_without_migrations_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    spec = make_spec()  # no ``migrations`` field
    h.registry.resolved = make_resolved(spec, version="1.0.0")

    called: list[Any] = []

    async def _fake_apply(entries: Any) -> list:
        called.append(entries)
        return []

    monkeypatch.setattr(installer_base_module, "apply_migrations", _fake_apply)

    await h.installer().install("tai42/toolbox")

    assert called == [], "a plugin declaring no chain must not invoke the runner"
    assert "cm:write" in h.events  # the install still completes


async def test_migration_failure_aborts_without_manifest_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    spec = _spec_with_migrations()
    h.registry.resolved = make_resolved(spec, version="1.0.0")

    async def _boom(entries: Any) -> list:
        h.events.append("migrate")
        raise RuntimeError("migration boom")

    monkeypatch.setattr(installer_base_module, "apply_migrations", _boom)
    monkeypatch.setattr(installer_base_module, "installed_plugin_entry", lambda distribution: object())

    with pytest.raises(RuntimeError, match="migration boom"):
        await h.installer().install("tai42/acct")

    # The manifest was NEVER patched and no attribution row was written — no
    # half-active plugin.
    assert "cm:write" not in h.events
    assert h.svc.calls == 0
    assert h.store.rows == {}
    # The layer build was the only pip call: the unwind swapped the previous layer (none)
    # back with no pip call.
    assert h.events.count("pip") == 1
    assert layer_inputs(h) is None


_TAMPERED = (
    "component 'tai42-acct' version 1 ('baseline') was applied with checksum abc but is now def; "
    "the chain has been rewritten"
)


async def test_a_migration_refusal_reaches_the_operator_with_its_text_and_the_restored_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    h.registry.resolved = make_resolved(make_spec(), version="1.0.0")
    await h.installer().install("tai42/toolbox")
    before = layer_inputs(h)
    h.registry.resolved = make_resolved(_spec_with_migrations(), version="1.0.0")

    async def _tampered(entries: Any) -> list:
        raise ChecksumMismatchError(_TAMPERED)

    monkeypatch.setattr(installer_base_module, "apply_migrations", _tampered)
    monkeypatch.setattr(installer_base_module, "installed_plugin_entry", lambda distribution: object())

    with pytest.raises(PluginMigrationError) as caught:
        await h.installer().install("tai42/acct")

    assert layer_inputs(h) == before
    assert isinstance(caught.value.__cause__, ChecksumMismatchError)
    assert to_operation_error(caught.value).message == (
        f"tai42/acct: its schema migrations could not be applied: {_TAMPERED}\n"
        "the previous plugin environment was restored"
    )


async def test_update_runs_new_version_migrations_before_manifest_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    old_spec = make_spec(name="acct", package="tai42-acct", version="1.0.0")
    h.store.preload(old_spec, version="1.0.0")
    new_spec = _spec_with_migrations(version="2.0.0")
    h.registry.resolved = make_resolved(new_spec, version="2.0.0")

    async def _fake_apply(entries: Any) -> list:
        h.events.append("migrate")
        return []

    monkeypatch.setattr(installer_base_module, "apply_migrations", _fake_apply)
    monkeypatch.setattr(installer_base_module, "installed_plugin_entry", lambda distribution: object())

    await h.installer().update("tai42/acct")

    order = [e for e in h.events if e in ("pip", "migrate", "cm:write")]
    assert order == ["pip", "migrate", "cm:write"]


def _recording_apply(h: Harness, applied: list[list[MigrationEntry]]) -> Any:
    async def _apply(entries: list[MigrationEntry]) -> list[AppliedMigration]:
        h.events.append("migrate")
        applied.append(list(entries))
        return []

    return _apply


async def test_a_server_package_install_applies_its_chain_from_its_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    spec = _server_spec_with_migrations()
    h.registry.resolved = make_resolved(spec, version="1.0.0", contract_range=None)
    _lay_server_spec_and_chain(h, spec, monkeypatch)
    applied: list[list[MigrationEntry]] = []
    monkeypatch.setattr(installer_base_module, "apply_migrations", _recording_apply(h, applied))

    await h.installer().install("tai42/srvchain")

    assert [[entry.component for entry in entries] for entries in applied] == [[_SERVER_PACKAGE]]
    order = [e for e in h.events if e in ("pip", "migrate", "cm:write", "store:record")]
    assert order == ["pip", "migrate", "cm:write", "store:record"]


async def test_a_server_package_update_applies_the_new_version_s_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_version(monkeypatch)
    h = Harness()
    old = _server_spec_with_migrations(version="1.0.0")
    h.registry.resolved = make_resolved(old, version="1.0.0", contract_range=None)
    _lay_server_spec_and_chain(h, old, monkeypatch)
    applied: list[list[MigrationEntry]] = []
    monkeypatch.setattr(installer_base_module, "apply_migrations", _recording_apply(h, applied))
    await h.installer().install("tai42/srvchain")
    new = _server_spec_with_migrations(version="2.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0", contract_range=None)
    _lay_server_spec_and_chain(h, new, monkeypatch)
    h.events.clear()

    await h.installer().update("tai42/srvchain")

    assert [entry.component for entry in applied[-1]] == [_SERVER_PACKAGE]
    assert f"/servers/{_SERVER_PACKAGE}/current/" in str(applied[-1][0].migrations_dir)
    order = [e for e in h.events if e in ("pip", "migrate", "cm:write")]
    assert order == ["pip", "migrate", "cm:write"]
