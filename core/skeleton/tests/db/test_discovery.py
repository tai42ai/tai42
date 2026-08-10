"""Chain discovery (A3/A5): resolving skeleton and plugin migration chains into
runner entries.

No live Postgres — pure resolution against installed package metadata. The
skeleton and the ``tai42-skeleton`` distribution are guaranteed installed in the
test environment, so they double as a real fixture for the package-relative
resolution path. A configured default database (password only) lets the migrator
identity resolve without opening a connection.
"""

from __future__ import annotations

import pytest
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import MigrationDiscoveryError

from tai42_skeleton.db import (
    SKELETON_COMPONENT,
    all_migration_entries,
    installed_plugin_entries,
    plugin_migration_entry,
)
from tai42_skeleton.db.discovery import _import_package_for_distribution, skeleton_migrations_dir


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default database is configured by a password alone; the migrator identity
    # then resolves (no connection is opened while building entries).
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")


def _spec(*, package: str, migrations: str | None) -> PluginSpec:
    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": "widget",
            "package": package,
            "version": "1.0.0",
            "description": "A test plugin",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1.0",
            "categories": ["dev"],
            "provides": [{"kind": "tool", "name": "w", "module": "w.m", "description": "d"}],
            "migrations": migrations,
        }
    )


def test_skeleton_migrations_dir_exists() -> None:
    assert skeleton_migrations_dir().is_dir()


def test_import_package_resolves_normalized_distribution() -> None:
    # ``tai42-skeleton`` installs the ``tai42_skeleton`` import package.
    assert _import_package_for_distribution("tai42-skeleton") == "tai42_skeleton"
    # The lookup is normalization-insensitive (hyphen/underscore/case).
    assert _import_package_for_distribution("Tai42_Skeleton") == "tai42_skeleton"


def test_import_package_unknown_distribution_is_loud() -> None:
    with pytest.raises(MigrationDiscoveryError, match="not installed"):
        _import_package_for_distribution("tai42-no-such-plugin-xyz")


def test_plugin_entry_none_when_no_migrations_declared() -> None:
    assert plugin_migration_entry(_spec(package="tai42-skeleton", migrations=None)) is None


def test_plugin_entry_resolves_package_relative_directory() -> None:
    # Point a synthetic spec at the skeleton distribution's real chain directory.
    spec = _spec(package="tai42-skeleton", migrations="sql/migrations")
    entry = plugin_migration_entry(spec)
    assert entry is not None
    assert entry.component == "tai42-skeleton"  # component identity is the distribution
    assert entry.migrations_dir.is_dir()


async def test_all_entries_skeleton_only_without_installed_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.marketplace import store as mp_store

    class _EmptyStore:
        async def list_installed(self):
            return []

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _EmptyStore)

    entries = await all_migration_entries()
    assert [entry.component for entry in entries] == [SKELETON_COMPONENT]


async def test_installed_plugin_entries_reads_marketplace_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from tai42_skeleton.marketplace import store as mp_store

    with_chain = _spec(package="tai42-skeleton", migrations="sql/migrations").model_dump(mode="json")
    no_chain = _spec(package="tai42-skeleton", migrations=None).model_dump(mode="json")

    class _FakeStore:
        async def list_installed(self):
            return [SimpleNamespace(spec=with_chain), SimpleNamespace(spec=no_chain)]

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _FakeStore)

    entries = await installed_plugin_entries()
    # Only the record that declares a chain contributes an entry (opt-in).
    assert [entry.component for entry in entries] == ["tai42-skeleton"]


async def test_installed_plugin_entries_empty_when_database_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unconfigured default database owns no store, so there are no plugin chains.
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    assert await installed_plugin_entries() == []
