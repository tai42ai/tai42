"""The installer resolves a just-installed plugin's chain from its packaged spec, import-free.

After ``pip`` lands a plugin, the installer applies its declared chain before the manifest
patch activates it. The chain's folder is resolved the way ``tai db migrate`` resolves it:
off the installed distribution's metadata, at the directory holding its packaged
``tai-plugin.yml``, never by importing the plugin. The runner is faked; the distribution is
a real on-disk install in a scratch site directory.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import MigrationDiscoveryError, MigrationEntry
from tai42_kit.plugins import parse_plugin_spec
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.marketplace import installer_base as installer_base_module
from tai42_skeleton.marketplace.installer import Installer

from ..db import _chain_plugin

_RAISING_INIT = "raise RuntimeError('resolving the chain must never import the plugin')\n"


@pytest.fixture(autouse=True)
def _default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    reset_all_settings()


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[MigrationEntry]:
    """The entries the installer hands the runner."""
    seen: list[MigrationEntry] = []

    async def fake_apply(entries: list[MigrationEntry]) -> list[Any]:
        seen.extend(entries)
        return []

    monkeypatch.setattr(installer_base_module, "apply_migrations", fake_apply)
    return seen


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **layout: Any) -> tuple[PluginSpec, Path]:
    site = tmp_path / "site"
    spec_text = _chain_plugin.spec_yaml(migrations_component=None)
    package = _chain_plugin.lay_out(site, spec_text=spec_text, init_body=_RAISING_INIT, **layout)
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    return parse_plugin_spec(spec_text.encode(), source="registry"), package


async def test_installer_applies_the_chain_of_a_distribution_with_two_top_level_packages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, applied: list[MigrationEntry]
) -> None:
    spec, package = _install(monkeypatch, tmp_path, second_top_level="tai42_chainprobe_helpers")

    await Installer()._run_plugin_migrations(spec)

    assert [(entry.component, Path(str(entry.migrations_dir))) for entry in applied] == [
        (_chain_plugin.DIST, package / "migrations")
    ]
    assert _chain_plugin.TOP_LEVEL not in sys.modules


async def test_installer_resolves_the_chain_without_importing_the_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, applied: list[MigrationEntry]
) -> None:
    spec, package = _install(monkeypatch, tmp_path)

    await Installer()._run_plugin_migrations(spec)

    assert [Path(str(entry.migrations_dir)) for entry in applied] == [package / "migrations"]
    assert _chain_plugin.TOP_LEVEL not in sys.modules


async def test_installer_refuses_a_chain_whose_distribution_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, applied: list[MigrationEntry]
) -> None:
    spec = parse_plugin_spec(_chain_plugin.spec_yaml(migrations_component=None).encode(), source="registry")

    with pytest.raises(MigrationDiscoveryError, match=f"{_chain_plugin.DIST}.*not installed"):
        await Installer()._run_plugin_migrations(spec)

    assert applied == []
