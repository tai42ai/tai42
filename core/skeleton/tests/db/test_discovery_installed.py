"""``tai db migrate`` discovers the chain of every installed plugin, whatever the manifest loads.

A chain-declaring plugin installed into the environment (outside the plugins prefix, with
no marketplace install record, and named by no manifest) still has its chain discovered:
the schema follows what is installed, so the plugin's tables exist before any manifest
loads it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

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


def test_installed_plugin_not_named_by_the_manifest_is_discovered(installed_site: Path) -> None:
    from tai42_skeleton.db import discover_plugin_chains

    discovery = discover_plugin_chains()

    by_component = {entry.component: entry for entry in discovery.entries}
    assert _chain_plugin.DIST in by_component, sorted(by_component)
    assert Path(str(by_component[_chain_plugin.DIST].migrations_dir)) == installed_site / "migrations"
