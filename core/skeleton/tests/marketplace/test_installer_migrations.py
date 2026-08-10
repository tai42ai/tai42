"""The installer's plugin-migration step (A5): run the plugin's declared schema
chain AFTER the package lands and BEFORE the manifest patch activates it.

Fully faked seams (reusing the installer harness): the kit runner
(``apply_migrations``) and the entry resolver (``plugin_migration_entry``) are
patched so the tests pin the WIRING — the migrate call's ordering relative to the
manifest write, the opt-in skip for a plugin with no chain, and the fail-loud /
no-partial-state guarantee when a migration fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import AppliedMigration

from tai42_skeleton.marketplace import installer as installer_module
from tests.marketplace._specs import make_resolved, make_spec, tool_item
from tests.marketplace.test_installer import Harness


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


def _patch_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")


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

    monkeypatch.setattr(installer_module, "apply_migrations", _fake_apply)
    monkeypatch.setattr(installer_module, "plugin_migration_entry", lambda s: object())

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

    monkeypatch.setattr(installer_module, "apply_migrations", _fake_apply)

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

    monkeypatch.setattr(installer_module, "apply_migrations", _boom)
    monkeypatch.setattr(installer_module, "plugin_migration_entry", lambda s: object())

    with pytest.raises(RuntimeError, match="migration boom"):
        await h.installer().install("tai42/acct")

    # The manifest was NEVER patched and no attribution row was written — no
    # half-active plugin.
    assert "cm:write" not in h.events
    assert h.svc.calls == 0
    assert h.store.rows == {}
    # The just-installed package was unwound (install pip + uninstall pip).
    assert h.events.count("pip") == 2


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

    monkeypatch.setattr(installer_module, "apply_migrations", _fake_apply)
    monkeypatch.setattr(installer_module, "plugin_migration_entry", lambda s: object())

    await h.installer().update("tai42/acct")

    order = [e for e in h.events if e in ("pip", "migrate", "cm:write")]
    assert order == ["pip", "migrate", "cm:write"]
