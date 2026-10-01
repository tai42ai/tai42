"""Chain discovery (A3/A5): resolving skeleton and plugin migration chains into
runner entries.

No live Postgres — pure resolution against installed package metadata. The
skeleton and the ``tai42-skeleton`` distribution are guaranteed installed in the
test environment, so they double as a real fixture for the package-relative
resolution path. A configured default database (password only) lets the migrator
identity resolve without opening a connection.
"""

from __future__ import annotations

from pathlib import Path

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


async def test_all_entries_two_skeleton_chains_without_installed_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.marketplace import store as mp_store
    from tai42_skeleton.states.db import STATES_COMPONENT

    class _EmptyStore:
        async def list_installed(self):
            return []

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _EmptyStore)

    entries = await all_migration_entries()
    # The two skeleton-owned chains, in order: the skeleton baseline first (plugin discovery
    # reads skeleton-owned tables), then the states record store.
    assert [entry.component for entry in entries] == [SKELETON_COMPONENT, STATES_COMPONENT]


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


async def test_discover_plugin_chains_carries_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plugin declaring a ``migrations_component`` override whose binding is unset is
    # carried on the discovery result's ``.skipped`` and omitted from ``.entries``;
    # ``installed_plugin_entries()`` keeps its signature and returns only ``.entries``.
    from types import SimpleNamespace

    from tai42_skeleton.db import SkippedChain, discover_plugin_chains
    from tai42_skeleton.marketplace import store as mp_store

    monkeypatch.delenv("TAI_DB_BINDING_ACME_ALERTS", raising=False)
    override_skip = _spec(package="tai42-skeleton", migrations="sql/migrations").model_dump(mode="json")
    override_skip["migrations_component"] = "acme_alerts"
    plain_chain = _spec(package="tai42-skeleton", migrations="sql/migrations").model_dump(mode="json")

    class _FakeStore:
        async def list_installed(self):
            return [SimpleNamespace(spec=override_skip), SimpleNamespace(spec=plain_chain)]

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _FakeStore)

    discovery = await discover_plugin_chains()
    assert [entry.component for entry in discovery.entries] == ["tai42-skeleton"]
    assert discovery.skipped == [SkippedChain(component="acme_alerts")]
    # The entry-only accessor stays signature-stable: only the runnable entries.
    assert [entry.component for entry in await installed_plugin_entries()] == ["tai42-skeleton"]


async def test_installed_plugin_entries_empty_when_database_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unconfigured default database owns no store, so there are no plugin chains.
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    assert await installed_plugin_entries() == []


async def test_installed_plugin_entries_tolerate_missing_store_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # A database not yet migrated to the skeleton baseline has no
    # ``marketplace_installs`` table: no store rows, never a discovery crash.
    from psycopg.errors import UndefinedTable

    from tai42_skeleton.marketplace import store as mp_store

    class _NoTableStore:
        async def list_installed(self):
            raise UndefinedTable('relation "marketplace_installs" does not exist')

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _NoTableStore)

    assert await installed_plugin_entries() == []


# --- prefix scan ----------------------------------------------------------


_WIDGET_SPEC_YAML = """\
spec_version: 1
namespace: tai42
name: widget
package: tai42-widget
version: 1.0.0
description: A prefix-installed test plugin
license: Apache-2.0
contract: ">=0.1,<1.0"
categories: [dev]
provides:
  - kind: tool
    name: w
    module: w.m
    description: d
migrations: migrations
"""


def _prefix_site(prefix: Path) -> Path:
    from tai42_skeleton.marketplace.prefix import prefix_site_dirs

    site = Path(prefix_site_dirs(str(prefix))[0])
    site.mkdir(parents=True, exist_ok=True)
    return site


def _write_dist_info(site: Path, dist_name: str, version: str, files: list[Path]) -> None:
    """A pip-shaped ``.dist-info`` for a distribution laid out under ``site``:
    ``METADATA`` naming the distribution and a ``RECORD`` listing its files (the
    listing ``importlib.metadata`` resolves ``Distribution.files`` from)."""
    dist_info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n")
    record_rows = [f"{file.relative_to(site)},," for file in files]
    record_rows.append(f"{dist_info.relative_to(site)}/METADATA,,")
    record_rows.append(f"{dist_info.relative_to(site)}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(record_rows) + "\n")


def _install_widget_into_prefix(prefix: Path, *, spec_yaml: str = _WIDGET_SPEC_YAML) -> Path:
    """Lay a ``tai42-widget`` distribution into the prefix's site dir the way a
    ``pip install --prefix`` leaves one: package tree + spec + chain + dist-info."""
    site = _prefix_site(prefix)
    pkg = site / "tai42_widget"
    (pkg / "migrations").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "tai-plugin.yml").write_text(spec_yaml)
    (pkg / "migrations" / "0001_baseline.sql").write_text("CREATE TABLE widget_items (id integer PRIMARY KEY);\n")
    _write_dist_info(
        site,
        "tai42-widget",
        "1.0.0",
        [pkg / "__init__.py", pkg / "tai-plugin.yml", pkg / "migrations" / "0001_baseline.sql"],
    )
    return pkg


@pytest.fixture
def _empty_store(monkeypatch: pytest.MonkeyPatch):
    from tai42_skeleton.marketplace import store as mp_store

    class _EmptyStore:
        async def list_installed(self):
            return []

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _EmptyStore)


def _configure_prefix(monkeypatch: pytest.MonkeyPatch, prefix: Path) -> None:
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: str(prefix))


@pytest.mark.usefixtures("_empty_store")
async def test_prefix_preinstalled_plugin_discovered_without_store_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A plugin pip-installed into the prefix has NO marketplace install row; its
    # declared chain is discovered from the prefix scan alone, resolved against the
    # prefix filesystem (the distribution is not importable from ``sys.path``).
    pkg = _install_widget_into_prefix(tmp_path)
    _configure_prefix(monkeypatch, tmp_path)

    entries = await installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-widget"]
    assert Path(str(entries[0].migrations_dir)) == pkg / "migrations"
    assert entries[0].migrations_dir.is_dir()


async def test_prefix_and_store_same_plugin_yield_one_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The same installed artifact seen from BOTH sources (a marketplace install
    # into a configured prefix) is one entry — resolved from the prefix filesystem,
    # which never needs the distribution importable in this process.
    from types import SimpleNamespace

    import yaml

    from tai42_skeleton.marketplace import store as mp_store

    pkg = _install_widget_into_prefix(tmp_path)
    _configure_prefix(monkeypatch, tmp_path)
    record_spec = yaml.safe_load(_WIDGET_SPEC_YAML)

    class _FakeStore:
        async def list_installed(self):
            return [SimpleNamespace(spec=record_spec, ref="tai42/widget")]

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _FakeStore)

    entries = await installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-widget"]
    assert Path(str(entries[0].migrations_dir)) == pkg / "migrations"


@pytest.mark.usefixtures("_empty_store")
async def test_prefix_dependency_without_spec_contributes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A dependency distribution in the prefix ships no packaged spec: skipped.
    site = _prefix_site(tmp_path)
    pkg = site / "some_helper"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    _write_dist_info(site, "some-helper", "2.0.0", [pkg / "__init__.py"])
    _configure_prefix(monkeypatch, tmp_path)

    assert await installed_plugin_entries() == []


@pytest.mark.usefixtures("_empty_store")
async def test_prefix_plugin_without_chain_contributes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A prefix plugin whose spec declares no ``migrations`` chain owns no schema.
    spec_yaml = _WIDGET_SPEC_YAML.replace("migrations: migrations\n", "")
    _install_widget_into_prefix(tmp_path, spec_yaml=spec_yaml)
    _configure_prefix(monkeypatch, tmp_path)

    assert await installed_plugin_entries() == []


@pytest.mark.usefixtures("_empty_store")
async def test_prefix_malformed_spec_is_a_loud_discovery_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A prefix distribution shipping a schema-invalid spec fails discovery as a
    # clean MigrationDiscoveryError naming the distribution — never a raw traceback.
    _install_widget_into_prefix(tmp_path, spec_yaml="spec_version: 1\nname: widget\n")
    _configure_prefix(monkeypatch, tmp_path)

    with pytest.raises(MigrationDiscoveryError, match="tai42-widget"):
        await installed_plugin_entries()


async def test_store_malformed_spec_is_a_loud_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A store record whose persisted spec no longer validates fails discovery as a
    # clean MigrationDiscoveryError naming the distribution — never a raw traceback.
    from types import SimpleNamespace

    from tai42_skeleton.marketplace import store as mp_store

    class _BadSpecStore:
        async def list_installed(self):
            return [SimpleNamespace(spec={"spec_version": 1, "package": "tai42-widget"})]

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _BadSpecStore)

    with pytest.raises(MigrationDiscoveryError, match="tai42-widget"):
        await installed_plugin_entries()


@pytest.mark.usefixtures("_empty_store")
async def test_prefix_unset_or_empty_is_no_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No prefix configured: the scan contributes nothing.
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    assert await installed_plugin_entries() == []
    # A configured prefix whose site dirs do not exist (never installed into).
    _configure_prefix(monkeypatch, tmp_path / "never-created")
    assert await installed_plugin_entries() == []


# --- manifest source ------------------------------------------------------


class _FakeConfigManager:
    """A config-manager stand-in whose ``read_manifest`` returns a fixed document."""

    def __init__(self, document: dict) -> None:
        self._document = document

    def read_manifest(self) -> dict:
        return self._document


def _set_manifest(monkeypatch: pytest.MonkeyPatch, document: dict) -> None:
    from tai42_skeleton.config import ConfigManagerFactory

    monkeypatch.setattr(ConfigManagerFactory, "create", lambda: _FakeConfigManager(document))


def _map_module_to_skeleton_chain(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Resolve ``module``'s packaged spec to the installed ``tai42-skeleton`` chain.

    The real packaged-spec resolver is exercised by the mount-map tests; here the manifest
    source's fold is exercised against a spec that resolves to a real installed
    distribution (``tai42-skeleton``'s own ``sql/migrations``), so no live plugin install
    is needed. A module other than ``module`` ships no spec (an operator-authored module).
    """
    from tai42_skeleton.app import mount_map

    monkeypatch.setattr(
        mount_map,
        "_packaged_spec_for_module",
        lambda name: _spec(package="tai42-skeleton", migrations="sql/migrations") if name == module else None,
    )


@pytest.mark.usefixtures("_empty_store")
async def test_manifest_loaded_plugin_discovered_without_store_or_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # A chain-declaring plugin the manifest LOADS, with no store row and no prefix, is
    # discovered so the runner migrates exactly what boot will gate on.
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    _set_manifest(monkeypatch, {"lifecycle_modules": ["tai42_widget.lifecycle"]})
    _map_module_to_skeleton_chain(monkeypatch, "tai42_widget.lifecycle")

    entries = await installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-skeleton"]
    assert entries[0].migrations_dir.is_dir()


@pytest.mark.usefixtures("_empty_store")
async def test_manifest_loaded_from_every_role_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # A chain-declaring plugin reaches discovery whichever role list the manifest loads it
    # under — the source mirrors the component importer's full set, not just one field.
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    for field, value, module in (
        ("webhook_verifier_modules", ["tai42_widget.verify"], "tai42_widget.verify"),
        ("channel_modules", ["tai42_widget.channel"], "tai42_widget.channel"),
        ("routers_modules", ["tai42_widget.routes"], "tai42_widget.routes"),
        ("middlewares_modules", ["tai42_widget.mw"], "tai42_widget.mw"),
        ("extensions_modules", ["tai42_widget.ext"], "tai42_widget.ext"),
        ("backend_module", "tai42_widget.backend", "tai42_widget.backend"),
        ("tools", [{"module": "tai42_widget.tools", "title": "Widget"}], "tai42_widget.tools"),
        ("agents", [{"module": "tai42_widget.agent", "title": "Widget"}], "tai42_widget.agent"),
    ):
        _set_manifest(monkeypatch, {field: value})
        _map_module_to_skeleton_chain(monkeypatch, module)
        entries = await installed_plugin_entries()
        assert [entry.component for entry in entries] == ["tai42-skeleton"], field


def test_manifest_source_skips_module_without_packaged_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest module whose top-level package ships no tai-plugin.yml (an
    # operator-authored module) yields no slot — never a guess, never a crash.
    from tai42_skeleton.db.discovery import _collect_manifest_sources

    _set_manifest(monkeypatch, {"lifecycle_modules": ["tai42_skeleton.app.instance"]})

    assert _collect_manifest_sources() == {}


def test_manifest_source_excludes_skeleton_routers_and_never_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    # The effective router set is composed of skeleton-owned modules that ship no
    # tai-plugin.yml, so the manifest source yields nothing for them; and because it maps
    # NAMED modules to their packaged spec (never scanning a site dir), it cannot trip the
    # prefix scan's several-tai-plugin.yml guard on tai42-cli's template specs.
    from tai42_skeleton.db.discovery import _collect_manifest_sources

    _set_manifest(monkeypatch, {"default_routers": "all"})

    assert _collect_manifest_sources() == {}


async def test_manifest_and_store_same_distribution_yield_one_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same installed distribution named by BOTH the manifest and the store is one
    # artifact: discovery yields a single entry, never a duplicate.
    from types import SimpleNamespace

    from tai42_skeleton.marketplace import store as mp_store

    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    store_spec = _spec(package="tai42-skeleton", migrations="sql/migrations").model_dump(mode="json")

    class _FakeStore:
        async def list_installed(self):
            return [SimpleNamespace(spec=store_spec)]

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _FakeStore)
    _set_manifest(monkeypatch, {"lifecycle_modules": ["tai42_widget.lifecycle"]})
    _map_module_to_skeleton_chain(monkeypatch, "tai42_widget.lifecycle")

    entries = await installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-skeleton"]


async def test_prefix_wins_over_manifest_for_same_distribution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A distribution the prefix carries (with a package root) AND the manifest loads is one
    # entry, resolved from the prefix filesystem — the root-carrying source is preferred.
    from tai42_skeleton.marketplace import store as mp_store

    class _EmptyStore:
        async def list_installed(self):
            return []

    monkeypatch.setattr(mp_store, "MarketplaceInstallStore", _EmptyStore)
    pkg = _install_widget_into_prefix(tmp_path)
    _configure_prefix(monkeypatch, tmp_path)
    _set_manifest(monkeypatch, {"lifecycle_modules": ["tai42_widget.lifecycle"]})
    # The manifest maps the module to the SAME distribution the prefix carries.
    from tai42_skeleton.app import mount_map

    def _resolver(name: str):
        return _spec(package="tai42-widget", migrations="migrations") if name == "tai42_widget.lifecycle" else None

    monkeypatch.setattr(mount_map, "_packaged_spec_for_module", _resolver)

    entries = await installed_plugin_entries()

    assert [entry.component for entry in entries] == ["tai42-widget"]
    # Resolved from the prefix filesystem, not installed metadata.
    assert Path(str(entries[0].migrations_dir)) == pkg / "migrations"


@pytest.mark.usefixtures("_empty_store")
async def test_absent_manifest_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A deployment that boots always has a manifest (boot reads it with no fallback), so a
    # missing one on the discovery path is corrupt configuration — a loud FileNotFoundError,
    # never a false zero-pending that reports green while the app refuses to boot.
    from tai42_skeleton.config import ConfigManagerFactory
    from tai42_skeleton.db import discover_plugin_chains

    def _absent() -> dict:
        raise FileNotFoundError("Manifest not found: /app/config/manifest.yml")

    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)

    class _AbsentManager:
        read_manifest = staticmethod(_absent)

    monkeypatch.setattr(ConfigManagerFactory, "create", lambda: _AbsentManager())

    with pytest.raises(FileNotFoundError, match="Manifest not found"):
        await discover_plugin_chains()
