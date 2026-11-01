"""``tai plugins rebuild`` — rebuild the plugin environments from the install store.

The plugins directory is real; the boot read, the fleet lock, the install store and pip are
fakes, pip being :class:`LayingPip`, which lays a layer build's distributions into its
``--prefix``. One test runs the real boot read over a file config store, so a setting kept only
in the store reaches the rebuild.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner, Result
from tai42_cli import app as app_module
from tai42_kit.db import component_binding, component_store_settings, database_password_env
from tai42_kit.settings import reset_all_settings
from tests._fakes.plugin_environment_dists import LayingPip
from tests.marketplace._specs import install_row, make_spec

from tai42_skeleton.app import instance
from tai42_skeleton.cli.native import plugins_rebuild
from tai42_skeleton.config.file_manager import FileConfigManager
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.marketplace.environments import rebuild as rebuild_module
from tai42_skeleton.marketplace.environments.settings import PLUGINS_DIR_UNSET
from tai42_skeleton.marketplace.errors import OperationInProgressError
from tai42_skeleton.marketplace.store import InstallRecord

_ROW = install_row(make_spec(name="toolbox", package="tai42-fixture-toolbox"))


class _Store:
    def __init__(self, rows: list[InstallRecord], events: list[str]) -> None:
        self._rows = rows
        self._events = events

    async def list_installed(self) -> list[InstallRecord]:
        self._events.append("store:list")
        return self._rows


class _BootRead:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def read_boot_manifest(self) -> None:
        self._events.append("boot:read")


@pytest.fixture(autouse=True)
def _faked_boot_read(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The boot read of a test that does not run the real one; restores the process env it may write."""
    from tai42_skeleton.app import applied_env

    snapshot = dict(os.environ)
    loaded_before = set(applied_env._loaded_env_keys)
    monkeypatch.setattr(instance, "app", SimpleNamespace(lifecycle=_BootRead([])), raising=False)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = loaded_before
    reset_all_settings()


def _seams(monkeypatch: pytest.MonkeyPatch, plugins_dir: Path, rows: list[InstallRecord], pip: LayingPip) -> list[str]:
    """Serve the command from ``rows`` and ``pip`` under a recording fleet lock; return the events."""
    events: list[str] = []
    monkeypatch.setattr(instance, "app", SimpleNamespace(lifecycle=_BootRead(events)), raising=False)

    @asynccontextmanager
    async def _lock():
        events.append("lock:acquire")
        yield
        events.append("lock:release")

    monkeypatch.setattr(plugins_rebuild, "configured_plugins_dir", lambda: plugins_dir)
    monkeypatch.setattr(plugins_rebuild, "fleet_lock", _lock)
    monkeypatch.setattr(plugins_rebuild, "MarketplaceInstallStore", lambda: _Store(rows, events))
    monkeypatch.setattr(plugins_rebuild, "run_pip", pip)
    monkeypatch.setattr(rebuild_module, "ensure_pip_available", lambda: None)
    return events


def _invoke() -> Result:
    return CliRunner().invoke(app_module.app, ["plugins", "rebuild"])


def test_a_rebuild_prints_each_environment_and_that_servers_load_it_on_their_next_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _seams(monkeypatch, tmp_path, [_ROW], LayingPip([("tai42-fixture-toolbox", "1.0.0", [])]))

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert events == ["boot:read", "lock:acquire", "store:list", "lock:release"]
    assert "layer" in result.output
    assert "rebuilt" in result.output
    assert result.output.rstrip().endswith(plugins_rebuild.NEXT_START)


def test_an_up_to_date_directory_prints_up_to_date_and_no_next_start_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seams(monkeypatch, tmp_path, [], LayingPip())

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert plugins_rebuild.NEXT_START not in result.output


def test_an_unset_plugins_directory_exits_non_zero_naming_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins_rebuild, "configured_plugins_dir", lambda: None)

    result = _invoke()

    assert result.exit_code == 1
    assert f"Error: {PLUGINS_DIR_UNSET}" in result.output


def test_another_marketplace_operation_holding_the_lock_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seams(monkeypatch, tmp_path, [_ROW], LayingPip())

    @asynccontextmanager
    async def _held():
        raise OperationInProgressError("another marketplace operation is in progress")
        yield

    monkeypatch.setattr(plugins_rebuild, "fleet_lock", _held)

    result = _invoke()

    assert result.exit_code == 1
    assert "Error: another marketplace operation is in progress" in result.output


def test_a_refusal_prints_the_notes_added_to_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seams(monkeypatch, tmp_path, [_ROW], LayingPip())
    refusal = OperationInProgressError("another marketplace operation is in progress")
    refusal.add_note("a note added to the refusal")

    @asynccontextmanager
    async def _held():
        raise refusal
        yield

    monkeypatch.setattr(plugins_rebuild, "fleet_lock", _held)

    result = _invoke()

    assert result.exit_code == 1
    assert "Error: another marketplace operation is in progress\na note added to the refusal" in result.output


def test_a_build_failure_exits_non_zero_with_pip_s_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seams(monkeypatch, tmp_path, [_ROW], LayingPip(fail="ERROR: No matching distribution found for tai42-fixture"))

    result = _invoke()

    assert result.exit_code == 1
    assert "exited with code 1" in result.output
    assert "ERROR: No matching distribution found for tai42-fixture" in result.output


class _SettingsReadingStore:
    """An install store that resolves the database settings on its first read, as the real one does."""

    async def list_installed(self) -> list[InstallRecord]:
        component_store_settings(SKELETON_COMPONENT)
        return []


def test_a_rebuild_reads_a_database_setting_kept_only_in_the_env_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password_env = database_password_env(component_binding(SKELETON_COMPONENT))
    for key in (password_env, "TAI_BUS_REDIS_URL", "TAI_MANIFEST_PATH"):
        monkeypatch.delenv(key, raising=False)
    reset_all_settings()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "manifest.yml").write_text("default_routers: none\n")
    manager = FileConfigManager(config_dir_path=str(config_dir))
    manager.write_env({password_env: "stored-only"})
    _seams(monkeypatch, tmp_path / "plugins", [], LayingPip())
    monkeypatch.delattr(instance, "app", raising=False)
    monkeypatch.setattr(instance.app, "_config_manager", manager)
    monkeypatch.setattr(instance.app, "_bus", None)
    monkeypatch.setattr(plugins_rebuild, "MarketplaceInstallStore", _SettingsReadingStore)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_a_manifest_that_does_not_exist_exits_non_zero_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in ("TAI_BUS_REDIS_URL", "TAI_MANIFEST_PATH"):
        monkeypatch.delenv(key, raising=False)
    reset_all_settings()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _seams(monkeypatch, tmp_path / "plugins", [], LayingPip())
    monkeypatch.delattr(instance, "app", raising=False)
    monkeypatch.setattr(instance.app, "_config_manager", FileConfigManager(config_dir_path=str(config_dir)))
    monkeypatch.setattr(instance.app, "_bus", None)

    result = _invoke()

    assert result.exit_code == 1, result.output
    assert result.output.startswith(f"Error: Manifest not found: {config_dir / 'manifest.yml'}"), result.output
