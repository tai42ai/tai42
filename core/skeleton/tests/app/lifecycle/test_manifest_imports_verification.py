"""The preflight's last step: the first call in a process verifies the plugin environments.

The plugins directory is a real, empty one; the install store's rows, the shared fleet
lock's connection and the migration status are faked, and both gates read the real
configured-database verdict, so each case records whether the lock was taken and the rows
were read.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import tai42_kit.db.migrations as kit_migrations
from tai42_kit.db import (
    ComponentStatus,
    MigrationScript,
    SchemaOutOfDateError,
    component_store_configured,
    database_password_env,
)
from tai42_kit.settings import reset_all_settings
from tai42_kit.transport import stdio_launch as stdio_launch_module

import tai42_skeleton.db.boot_gate as boot_gate
from tai42_skeleton.app.lifecycle.manifest_imports import prepare_manifest_imports
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.compat import CorePluginBootError
from tai42_skeleton.marketplace.environments import activate, boot_verification
from tai42_skeleton.marketplace.environments.layout import BUILD_RECORD, layer_dir
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.environments.verify import verify_environments
from tai42_skeleton.marketplace.store import InstallRecord, MarketplaceInstallStore

from ...marketplace._specs import install_row, make_spec

_MANIFEST = Manifest.model_validate({"default_routers": "none"})
_PENDING = MigrationScript(version=2, name="add_thing", filename="0002_add_thing.sql", checksum="c", sql="")


class _Deployment:
    """The faked database side: the rows the store returns, the chain's pending scripts, what was done."""

    def __init__(self) -> None:
        self.rows: list[InstallRecord] = []
        self.pending: tuple[MigrationScript, ...] = ()
        self.events: list[str] = []


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Deployment]:
    faked = _Deployment()

    class _Cursor:
        async def execute(self, sql: str, params: object) -> None:
            faked.events.append(sql.split("(")[0].removeprefix("SELECT "))

    class _Connection:
        async def set_autocommit(self, value: bool) -> None:
            pass

        @asynccontextmanager
        async def cursor(self) -> AsyncIterator[_Cursor]:
            yield _Cursor()

    @asynccontextmanager
    async def _pinned_connection(settings: object) -> AsyncIterator[_Connection]:
        yield _Connection()

    async def _list_installed(self: MarketplaceInstallStore) -> list[InstallRecord]:
        faked.events.append("rows")
        return faked.rows

    async def _status(entries):
        return [
            ComponentStatus(component=entry.component, applied_versions=(1,), pending=faked.pending, mismatches=())
            for entry in entries
        ]

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setenv(database_password_env("default"), "pw")
    monkeypatch.setenv("TAI_PLUGINS_DIR", str(plugins_dir))
    reset_all_settings()
    monkeypatch.setattr(locks, "pinned_connection", _pinned_connection)
    monkeypatch.setattr(MarketplaceInstallStore, "list_installed", _list_installed)
    monkeypatch.setattr(kit_migrations, "migration_status", _status)
    monkeypatch.setattr(activate._ACTIVE, "target", None)
    monkeypatch.setattr(stdio_launch_module, "_LAUNCH", stdio_launch_module._no_exposed_commands)
    monkeypatch.setattr(boot_verification._VERIFIED, "done", False)
    # Both gates read the real configured-database verdict here; the suite's offline
    # posture reports every database unconfigured to them.
    monkeypatch.setattr(boot_gate, "component_store_configured", component_store_configured)
    monkeypatch.setattr(boot_verification, "component_store_configured", component_store_configured)
    yield faked
    reset_all_settings()


def _prepare() -> None:
    asyncio.run(prepare_manifest_imports(_MANIFEST))


def test_the_first_call_refuses_a_plugins_directory_the_install_store_does_not_match(
    deployment: _Deployment,
) -> None:
    deployment.rows = [install_row(make_spec())]

    with pytest.raises(CorePluginBootError) as caught:
        _prepare()

    assert str(caught.value).startswith("plugin environment is out of date — refusing to start")
    assert str(caught.value).endswith(". Run 'tai plugins rebuild' against this deployment, then start again")
    assert deployment.events == ["pg_advisory_lock_shared", "rows", "pg_advisory_unlock_shared"]


def test_a_later_call_in_the_same_process_does_not_verify(deployment: _Deployment) -> None:
    _prepare()
    deployment.events.clear()
    # A reload sees the directory the fleet's writer produced; it is not verified again.
    deployment.rows = [install_row(make_spec())]

    _prepare()

    assert deployment.events == []


def test_a_pending_skeleton_chain_is_refused_before_the_environments_are_verified(deployment: _Deployment) -> None:
    deployment.rows = [install_row(make_spec())]
    deployment.pending = (_PENDING,)

    with pytest.raises(SchemaOutOfDateError):
        _prepare()

    assert deployment.events == []


def test_with_no_skeleton_database_the_preflight_passes_without_reading_the_store(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(database_password_env("default"))
    reset_all_settings()

    _prepare()

    assert deployment.events == []


@pytest.mark.parametrize("record", ["{not json", None], ids=["unreadable", "missing"])
def test_a_layer_record_that_does_not_read_is_refused_first_in_the_verification_words(
    deployment: _Deployment, tmp_path: Path, record: str | None
) -> None:
    plugins_dir = tmp_path / "plugins"
    environment = layer_dir(plugins_dir)
    build = environment.build("b1")
    build.mkdir(parents=True)
    if record is not None:
        (build / BUILD_RECORD).write_text(record, encoding="utf-8")
    point_current(environment, "b1")
    [finding] = verify_environments(plugins_dir, []).findings

    with pytest.raises(CorePluginBootError) as caught:
        _prepare()

    # The record is read for the import snapshot, before the gates: the store is not read.
    assert str(caught.value) == finding.boot_text()
    assert deployment.events == []
