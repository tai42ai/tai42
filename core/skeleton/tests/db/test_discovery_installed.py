"""``tai db migrate`` discovers the chain of every installed plugin, whatever the manifest loads.

A chain-declaring plugin installed into the image (outside the plugin layer, with
no marketplace install record, and named by no manifest) still has its chain discovered:
the schema follows what is installed, so the plugin's tables exist before any manifest
loads it. One read of the installed metadata serves both the installed scan and the plugins
the manifest loads, and a packaged spec this contract cannot parse from a plugin built for
another contract is refused naming the declared range.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_kit.db import MigrationDiscoveryError
from tests._fakes.plugin_environment_dists import current_layer_build, write_venv_config

from tai42_skeleton.marketplace.compat import running_contract_version

from . import _chain_plugin


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default database is configured by a password alone; the migrator identity
    # then resolves (no connection is opened while building entries).
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr("tai42_skeleton.marketplace.environments.current_builds.configured_plugins_dir", lambda: None)


@pytest.fixture
def installed_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The neutral plugin installed into a site directory on ``sys.path``; returns its package dir."""
    site = tmp_path / "site"
    package = _chain_plugin.lay_out(site, spec_text=_chain_plugin.spec_yaml(migrations_component=None))
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    return package


class _ManifestLoadingThePlugin:
    """A config-manager stand-in whose manifest loads the plugin's tools module."""

    def read_manifest(self) -> dict:
        return {"default_routers": "none", "tools": [{"title": "Probe", "module": _chain_plugin.TOOLS_MODULE}]}


def _lay_out_on_path(monkeypatch: pytest.MonkeyPatch, site: Path, **lay_out: object) -> Path:
    package = _chain_plugin.lay_out(site, **lay_out)  # type: ignore[arg-type]
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    return package


def test_installed_plugin_not_named_by_the_manifest_is_discovered(installed_site: Path) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    discovery = discover_plugin_chains()

    by_component = {entry.component: entry for entry in discovery.entries}
    assert _chain_plugin.DIST in by_component, sorted(by_component)
    assert Path(str(by_component[_chain_plugin.DIST].migrations_dir)) == installed_site / "migrations"


def test_one_metadata_read_serves_the_installed_scan_and_the_manifest_route(
    monkeypatch: pytest.MonkeyPatch, installed_site: Path
) -> None:
    from tai42_skeleton.config import ConfigManagerFactory
    from tai42_skeleton.db import discover_plugin_chains

    monkeypatch.setattr(ConfigManagerFactory, "create", lambda: _ManifestLoadingThePlugin())
    real_distributions = importlib.metadata.distributions
    full_scans: list[object] = []

    def counting(**kwargs: object) -> Iterator[importlib.metadata.Distribution]:
        if "name" not in kwargs:
            full_scans.append(kwargs.get("path"))
        return iter(real_distributions(**kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.metadata, "distributions", counting)

    discovery = discover_plugin_chains()

    assert _chain_plugin.DIST in {entry.component for entry in discovery.entries}
    assert len(full_scans) == 1, full_scans


_UNKNOWN_KEY_SPEC = _chain_plugin.spec_yaml(migrations_component=None) + "future_key: 1\n"


def test_unreadable_spec_of_a_plugin_for_another_contract_names_the_declared_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    _lay_out_on_path(monkeypatch, tmp_path / "site", spec_text=_UNKNOWN_KEY_SPEC, requires_contract=">=999")

    with pytest.raises(MigrationDiscoveryError) as raised:
        discover_plugin_chains()

    message = str(raised.value)
    assert f"{_chain_plugin.DIST} requires tai42-contract >=999, but {running_contract_version()} is running" in message
    assert "Extra inputs are not permitted" not in message


def test_unreadable_spec_of_a_plugin_in_the_layer_names_the_declared_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tai42_skeleton.db import discover_plugin_chains
    from tai42_skeleton.marketplace.environments.layout import layer_site_dirs

    plugins_dir = tmp_path / "plugins"
    site = layer_site_dirs(current_layer_build(plugins_dir))[0]
    _chain_plugin.lay_out(site, spec_text=_UNKNOWN_KEY_SPEC, requires_contract=">=999")
    monkeypatch.setattr(
        "tai42_skeleton.marketplace.environments.current_builds.configured_plugins_dir", lambda: plugins_dir
    )

    with pytest.raises(MigrationDiscoveryError, match=f"{_chain_plugin.DIST} requires tai42-contract >=999"):
        discover_plugin_chains()


def test_layer_only_plugin_gets_the_snapshot_verdict_on_the_manifest_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A plugin installed only in the layer (never on this process's sys.path) is judged by
    # the snapshot's read of it: its declared range rules it incompatible, so the manifest
    # route does not read its spec.
    from tai42_skeleton.db.discovery import manifest_plugin_specs
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.marketplace.compat import module_compat
    from tai42_skeleton.marketplace.environments.current_builds import process_search_path
    from tai42_skeleton.marketplace.environments.layout import layer_site_dirs
    from tai42_skeleton.marketplace.installed_packages import installed_packages

    plugins_dir = tmp_path / "plugins"
    site = layer_site_dirs(current_layer_build(plugins_dir))[0]
    _chain_plugin.lay_out(site, spec_text=_chain_plugin.spec_yaml(), requires_contract=">=999")
    monkeypatch.setattr(
        "tai42_skeleton.marketplace.environments.current_builds.configured_plugins_dir", lambda: plugins_dir
    )
    snapshot = installed_packages(process_search_path())
    manifest = Manifest.model_validate(
        {"default_routers": "none", "tools": [{"title": "Probe", "module": _chain_plugin.TOOLS_MODULE}]}
    )

    verdict = module_compat(_chain_plugin.TOOLS_MODULE, snapshot)

    assert verdict.status == "incompatible", verdict
    assert f"{_chain_plugin.DIST} requires tai42-contract >=999" in (verdict.reason or "")
    assert manifest_plugin_specs(manifest, snapshot) == {}


def test_unreadable_spec_of_a_compatible_plugin_names_the_parse_fault(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    _lay_out_on_path(monkeypatch, tmp_path / "site", spec_text=_UNKNOWN_KEY_SPEC, requires_contract=">=0.1,<1000")

    with pytest.raises(MigrationDiscoveryError, match="Extra inputs are not permitted"):
        discover_plugin_chains()


def _put_nameless_dist_info_on_path(monkeypatch: pytest.MonkeyPatch, site: Path) -> Path:
    """A ``.dist-info`` that holds no ``METADATA``, in ``site`` on ``sys.path``; returns its path."""
    dist_info = site / "stray-1.0.0.dist-info"
    dist_info.mkdir(parents=True)
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    return dist_info


def test_boot_snapshot_names_a_distribution_recording_no_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tai42_skeleton.marketplace.compat import distribution_map
    from tai42_skeleton.marketplace.installed_packages import InstalledMetadataError

    dist_info = _put_nameless_dist_info_on_path(monkeypatch, tmp_path / "site")

    with pytest.raises(InstalledMetadataError, match=f"{dist_info} records no Name"):
        distribution_map()


def test_migrate_discovery_names_a_distribution_recording_no_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    dist_info = _put_nameless_dist_info_on_path(monkeypatch, tmp_path / "site")

    with pytest.raises(MigrationDiscoveryError, match=f"{dist_info} records no Name"):
        discover_plugin_chains()


def test_installer_entry_names_a_distribution_recording_no_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tai42_skeleton.db import installed_plugin_entry

    dist_info = _put_nameless_dist_info_on_path(monkeypatch, tmp_path / "site")

    with pytest.raises(MigrationDiscoveryError, match=f"{dist_info} records no Name"):
        installed_plugin_entry("stray")


_SERVER_BUILD = "20260101T000000Z-000000"


@pytest.fixture
def plugins_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A plugins directory ``TAI_PLUGINS_DIR`` names."""
    plugins = tmp_path / "plugins"
    monkeypatch.setattr(
        "tai42_skeleton.marketplace.environments.current_builds.configured_plugins_dir", lambda: plugins
    )
    return plugins


def _server_build(plugins_dir: Path, python: str | None = None) -> Path:
    """The chain-declaring server package's ``current`` build of Python ``python``, an empty venv site directory.

    Returns that site. ``python`` defaults to the running interpreter's version.
    """
    from tai42_skeleton.marketplace.environments.layout import server_dir
    from tai42_skeleton.marketplace.environments.swap import point_current

    environment = server_dir(plugins_dir, _chain_plugin.DIST)
    site = write_venv_config(environment.build(_SERVER_BUILD), python)
    point_current(environment, _SERVER_BUILD)
    return site


def _lay_out_server_package(plugins_dir: Path, python: str | None = None) -> Path:
    """The chain-declaring ``mcp-server`` package in its own ``current`` environment of Python ``python``.

    Returns its package dir, as seen through the ``current`` symlink, the path discovery reads.
    """
    from tai42_skeleton.marketplace.environments.layout import server_dir

    environment = server_dir(plugins_dir, _chain_plugin.DIST)
    package = _chain_plugin.lay_out(_server_build(plugins_dir, python), spec_text=_chain_plugin.server_spec_yaml())
    return environment.current / package.relative_to(environment.build(_SERVER_BUILD))


def test_a_server_package_s_chain_is_discovered_from_its_own_environment(plugins_dir: Path) -> None:
    from tai42_skeleton.db import discover_all_migration_chains

    package = _lay_out_server_package(plugins_dir)

    by_component = {entry.component: entry for entry in discover_all_migration_chains().entries}
    assert _chain_plugin.DIST in by_component, sorted(by_component)
    assert Path(str(by_component[_chain_plugin.DIST].migrations_dir)) == package / "migrations"


def test_a_server_package_built_by_an_older_python_is_discovered_from_its_own_site_dir(plugins_dir: Path) -> None:
    from tai42_skeleton.db import discover_all_migration_chains, installed_plugin_entry

    older = "3.11" if sys.version_info[:2] == (3, 12) else "3.12"
    package = _lay_out_server_package(plugins_dir, older)

    assert f"python{older}" in str(package)
    by_component = {entry.component: entry for entry in discover_all_migration_chains().entries}
    assert Path(str(by_component[_chain_plugin.DIST].migrations_dir)) == package / "migrations"
    entry = installed_plugin_entry(_chain_plugin.DIST)
    assert entry is not None
    assert Path(str(entry.migrations_dir)) == package / "migrations"


def test_installer_entry_reads_a_server_package_from_its_environment(plugins_dir: Path) -> None:
    from tai42_skeleton.db import installed_plugin_entry

    package = _lay_out_server_package(plugins_dir)

    entry = installed_plugin_entry(_chain_plugin.DIST)
    assert entry is not None
    assert entry.component == _chain_plugin.DIST
    assert Path(str(entry.migrations_dir)) == package / "migrations"


def test_a_server_package_also_on_the_search_path_is_read_from_its_environment(
    monkeypatch: pytest.MonkeyPatch, plugins_dir: Path, tmp_path: Path
) -> None:
    # The image holds the package at one version (one chain file); its server environment, which
    # runs it, holds another (two files): the chain discovered is the one that runs.
    from tai42_skeleton.db import discover_all_migration_chains, installed_plugin_entry

    _lay_out_on_path(monkeypatch, tmp_path / "site", spec_text=_chain_plugin.server_spec_yaml())
    package = _lay_out_server_package(plugins_dir)
    (package / "migrations" / "0002_more.sql").write_text("ALTER TABLE chainprobe_items ADD note text;\n")

    by_component = {entry.component: entry for entry in discover_all_migration_chains().entries}
    assert Path(str(by_component[_chain_plugin.DIST].migrations_dir)) == package / "migrations"
    entry = installed_plugin_entry(_chain_plugin.DIST)
    assert entry is not None
    assert Path(str(entry.migrations_dir)) == package / "migrations"


def test_a_server_environment_without_its_package_fails_migrate_discovery(plugins_dir: Path) -> None:
    from tai42_skeleton.db import discover_all_migration_chains
    from tai42_skeleton.marketplace.environments.layout import server_dir

    _server_build(plugins_dir)
    current = server_dir(plugins_dir, _chain_plugin.DIST).current

    with pytest.raises(
        MigrationDiscoveryError,
        match=(
            rf"^server build {re.escape(str(current))} holds no distribution {_chain_plugin.DIST}; "
            r"run 'tai plugins rebuild' against this deployment$"
        ),
    ):
        discover_all_migration_chains()
