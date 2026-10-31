"""``tai db migrate`` discovers the chain of every installed plugin, whatever the manifest loads.

A chain-declaring plugin installed into the environment (outside the plugins prefix, with
no marketplace install record, and named by no manifest) still has its chain discovered:
the schema follows what is installed, so the plugin's tables exist before any manifest
loads it. One read of the installed metadata serves both the installed scan and the plugins
the manifest loads, and a packaged spec this contract cannot parse from a plugin built for
another contract is refused naming the declared range.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_kit.db import MigrationDiscoveryError

from tai42_skeleton.marketplace.compat import running_contract_version

from . import _chain_plugin


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default database is configured by a password alone; the migrator identity
    # then resolves (no connection is opened while building entries).
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)


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


def test_unreadable_spec_of_a_plugin_in_the_prefix_names_the_declared_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tai42_skeleton.db import discover_plugin_chains
    from tai42_skeleton.marketplace.prefix import prefix_site_dirs

    prefix = tmp_path / "prefix"
    site = Path(prefix_site_dirs(str(prefix))[0])
    _chain_plugin.lay_out(site, spec_text=_UNKNOWN_KEY_SPEC, requires_contract=">=999")
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: str(prefix))

    with pytest.raises(MigrationDiscoveryError, match=f"{_chain_plugin.DIST} requires tai42-contract >=999"):
        discover_plugin_chains()


def test_prefix_only_plugin_gets_the_snapshot_verdict_on_the_manifest_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A plugin installed only in the prefix (never on this process's sys.path) is judged by
    # the snapshot's read of it: its declared range rules it incompatible, so the manifest
    # route does not read its spec.
    from tai42_skeleton.db.discovery import _installed_search_path, manifest_plugin_specs
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.marketplace.compat import module_compat
    from tai42_skeleton.marketplace.installed_packages import installed_packages
    from tai42_skeleton.marketplace.prefix import prefix_site_dirs

    prefix = tmp_path / "prefix"
    site = Path(prefix_site_dirs(str(prefix))[0])
    _chain_plugin.lay_out(site, spec_text=_chain_plugin.spec_yaml(), requires_contract=">=999")
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: str(prefix))
    snapshot = installed_packages(_installed_search_path())
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
