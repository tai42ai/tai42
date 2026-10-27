"""A plugin component whose database binding is not declared is OFF, and every db door says so.

A plugin's ``migrations_component`` names a separate, off-unless-declared database
component. With its ``TAI_DB_BINDING_*`` unset the component is off: its chain is not
applied anywhere, and that is a valid deployment, so ``tai db migrate``, ``--plan``,
``tai db status`` and ``tai doctor`` succeed and name the off component instead of
failing. The plugin is a real on-disk install in a scratch site directory, named by no
manifest; the PG-touching runner calls are faked.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from click.testing import CliRunner
from tai42_cli import app as app_module
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import AppliedMigration, ComponentStatus, MigrationEntry
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.cli.native import db, doctor

from ..db import _chain_plugin

_TARGET = cast("PostgresConnectionSettings", SimpleNamespace(pg_host="db", pg_port=5432, pg_db="tai"))


@pytest.fixture(autouse=True)
def installed_override_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The neutral plugin, declaring a ``migrations_component`` override, installed with its binding unset."""
    site = tmp_path / "site"
    _chain_plugin.lay_out(site, spec_text=_chain_plugin.spec_yaml())
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.delenv(_chain_plugin.BINDING_ENV, raising=False)
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    monkeypatch.setattr(db, "component_migrator_settings", lambda component: _TARGET)
    reset_all_settings()


def _up_to_date(entries: list[MigrationEntry]) -> list[ComponentStatus]:
    return [
        ComponentStatus(component=entry.component, applied_versions=(1,), pending=(), mismatches=())
        for entry in entries
    ]


def _assert_names_the_off_component(output: str) -> None:
    assert repr(_chain_plugin.COMPONENT) in output
    assert "is off" in output
    assert "TAI_DB_BINDING_" in output


def test_migrate_succeeds_and_names_the_off_component(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []

    async def fake_apply(entries: list[MigrationEntry]) -> list[AppliedMigration]:
        applied.extend(entry.component for entry in entries)
        return [AppliedMigration(entry.component, 1, "baseline", "abc") for entry in entries]

    monkeypatch.setattr(db, "apply_migrations", fake_apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert _chain_plugin.COMPONENT not in applied
    assert "Applied skeleton 0001_baseline." in result.output
    _assert_names_the_off_component(result.output)


def test_migrate_json_succeeds_and_lists_the_off_component(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_apply(entries: list[MigrationEntry]) -> list[AppliedMigration]:
        return []

    monkeypatch.setattr(db, "apply_migrations", fake_apply)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "migrate"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["skipped_chains"] == [{"component": _chain_plugin.COMPONENT}]


def test_migrate_plan_succeeds_and_names_the_off_component(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        return _up_to_date(entries)

    monkeypatch.setattr(db, "migration_status", fake_status)

    result = CliRunner().invoke(app_module.app, ["db", "migrate", "--plan"])

    assert result.exit_code == 0, result.output
    _assert_names_the_off_component(result.output)


def test_status_succeeds_and_names_the_off_component(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        return _up_to_date(entries)

    monkeypatch.setattr(db, "migration_status", fake_status)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 0, result.output
    _assert_names_the_off_component(result.output)


def test_status_json_succeeds_and_lists_the_off_component(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        return _up_to_date(entries)

    monkeypatch.setattr(db, "migration_status", fake_status)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "status"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["skipped_chains"] == [{"component": _chain_plugin.COMPONENT}]


def test_doctor_schema_probe_passes_and_names_the_off_component(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        return _up_to_date(entries)

    monkeypatch.setattr(doctor, "migration_status", fake_status)

    check = asyncio.run(doctor._probe_schema(_TARGET))

    assert check.status == doctor._OK, check.detail
    _assert_names_the_off_component(check.detail)
