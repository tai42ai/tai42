"""``tai db migrate`` / ``tai db status`` — apply and inspect migrations.

No live Postgres: the kit runner (``apply_migrations`` / ``migration_status``) and
the migrator settings are faked. The tests assert the command wiring — migrate
applies and reports, ``--plan`` inspects without applying, status is a clean/CI
gate — and the loud, credential-free failure surfaces (connection failure,
unconfigured connection, a rewritten chain).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import psycopg
import pytest
from click.testing import CliRunner
from tai42_cli import app as app_module
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import (
    AppliedMigration,
    ChecksumMismatchError,
    ComponentStatus,
    DatabaseNotConfiguredError,
    MigrationEntry,
    MigrationScript,
)

from tai42_skeleton.cli.native import db
from tai42_skeleton.db import MigrationChainDiscovery, SkippedChain

_TARGET = cast("PostgresConnectionSettings", SimpleNamespace(pg_host="db", pg_port=5432, pg_db="tai"))


def _script(version: int, name: str) -> MigrationScript:
    return MigrationScript(version=version, name=name, filename=f"{version:04d}_{name}.sql", checksum="abc", sql="")


def _status(component: str, *, applied: tuple[int, ...], pending: tuple[MigrationScript, ...] = ()) -> ComponentStatus:
    return ComponentStatus(component=component, applied_versions=applied, pending=pending, mismatches=())


@pytest.fixture(autouse=True)
def _fake_target(monkeypatch: pytest.MonkeyPatch) -> None:
    # The migrator target is only read for messages; the fakes make real connection
    # fields irrelevant and keep the credential-redaction assertions deterministic.
    # Chain discovery is stubbed so the command wiring is exercised without resolving
    # a real database or reading the marketplace store.
    monkeypatch.setattr(db, "component_migrator_settings", lambda component: _TARGET)
    monkeypatch.setattr(db, "component_binding", lambda component: "default")

    async def _empty_discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[])

    monkeypatch.setattr(db, "discover_all_migration_chains", _empty_discovery)
    monkeypatch.setattr(db, "discover_plugin_chains", _empty_discovery)
    monkeypatch.setattr(db, "skeleton_entry", lambda: SimpleNamespace(component="skeleton", settings=_TARGET))
    monkeypatch.setattr(db, "states_entry", lambda: SimpleNamespace(component="states", settings=_TARGET))


# --- migrate --------------------------------------------------------------


def test_migrate_applies_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _apply(entries: object) -> list[AppliedMigration]:
        return [AppliedMigration("skeleton", 1, "baseline", "abc")]

    monkeypatch.setattr(db, "apply_migrations", _apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert "Applied skeleton 0001_baseline" in result.output
    assert "Applied 1 migration(s)." in result.output


def test_migrate_applies_skeleton_before_plugin_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # Plugin discovery reads skeleton-owned tables, so migrate must land the
    # two skeleton-owned chains BEFORE it enumerates plugin chains — the order is
    # observable as apply(skeleton,states), then discovery, then apply(plugins).
    calls: list[str] = []

    async def _apply(entries: object) -> list[AppliedMigration]:
        components = ",".join(entry.component for entry in cast("list", entries))
        calls.append(f"apply:{components}")
        return []

    async def _plugin_discovery() -> MigrationChainDiscovery:
        calls.append("discover-plugins")
        entries = cast("list[MigrationEntry]", [SimpleNamespace(component="tai42-widget", settings=_TARGET)])
        return MigrationChainDiscovery(entries=entries, skipped=[])

    monkeypatch.setattr(db, "apply_migrations", _apply)
    monkeypatch.setattr(db, "discover_plugin_chains", _plugin_discovery)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert calls == ["apply:skeleton,states", "discover-plugins", "apply:tai42-widget"]


def test_migrate_json_reports_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _apply(entries: object) -> list[AppliedMigration]:
        return [AppliedMigration("skeleton", 1, "baseline", "abc")]

    monkeypatch.setattr(db, "apply_migrations", _apply)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "migrate"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == {"applied": [{"component": "skeleton", "version": 1, "name": "baseline"}], "skipped_chains": []}


def test_migrate_plan_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,), pending=(_script(2, "add_thing"),))]

    monkeypatch.setattr(db, "migration_status", _status_fn)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "migrate", "--plan"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"][0]["pending"] == "1"
    assert data["skipped_chains"] == []


def test_migrate_up_to_date_reports_nothing_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _apply(entries: object) -> list[AppliedMigration]:
        return []

    monkeypatch.setattr(db, "apply_migrations", _apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_migrate_names_skipped_chain_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    # A declared chain whose override binding is unset is carried as a skip: migrate
    # still reports what DID run, then names the skipped chain and exits non-zero.
    async def _apply(entries: object) -> list[AppliedMigration]:
        return [AppliedMigration("skeleton", 1, "baseline", "abc")]

    async def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[SkippedChain(component="acme_alerts")])

    monkeypatch.setattr(db, "apply_migrations", _apply)
    monkeypatch.setattr(db, "discover_plugin_chains", _discovery)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1, result.output
    # The migrations that DID run are still reported first.
    assert "Applied skeleton 0001_baseline" in result.output
    # The skipped chain is named, with its unset binding env var.
    assert "acme_alerts" in result.output
    assert "TAI_DB_BINDING" in result.output


def test_migrate_json_reports_skipped_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _apply(entries: object) -> list[AppliedMigration]:
        return [AppliedMigration("skeleton", 1, "baseline", "abc")]

    async def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[SkippedChain(component="acme_alerts")])

    monkeypatch.setattr(db, "apply_migrations", _apply)
    monkeypatch.setattr(db, "discover_plugin_chains", _discovery)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "migrate"])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["applied"] == [{"component": "skeleton", "version": 1, "name": "baseline"}]
    assert data["skipped_chains"] == [{"component": "acme_alerts"}]


def test_migrate_plan_names_skipped_chain_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,))]

    async def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[SkippedChain(component="acme_alerts")])

    async def _forbidden_apply(entries: object) -> list[AppliedMigration]:
        raise AssertionError("--plan must not apply anything")

    monkeypatch.setattr(db, "migration_status", _status_fn)
    monkeypatch.setattr(db, "discover_all_migration_chains", _discovery)
    monkeypatch.setattr(db, "apply_migrations", _forbidden_apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate", "--plan"])

    assert result.exit_code == 1, result.output
    assert "acme_alerts" in result.output
    assert "nothing applied (--plan)" in result.output


def test_migrate_plan_shows_pending_without_applying(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,), pending=(_script(2, "add_thing"),))]

    async def _forbidden_apply(entries: object) -> list[AppliedMigration]:
        raise AssertionError("--plan must not apply anything")

    monkeypatch.setattr(db, "migration_status", _status_fn)
    monkeypatch.setattr(db, "apply_migrations", _forbidden_apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate", "--plan"])

    assert result.exit_code == 0, result.output
    assert "1 pending migration(s)" in result.output
    assert "nothing applied (--plan)" in result.output


def test_migrate_loud_and_credential_free_on_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(entries: object) -> list[AppliedMigration]:
        raise psycopg.OperationalError("password authentication failed for user 'postgres' (s3cr3t)")

    monkeypatch.setattr(db, "apply_migrations", _boom)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1
    assert "could not connect to Postgres database 'default' at db:5432/tai" in result.output


def _closed_loopback_port() -> int:
    """A loopback port nothing listens on — a connect to it is refused at once."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_migrate_surfaces_the_real_connect_error_from_a_closed_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pooled client now proves its first connection at ``client_ctx`` entry, so a refused
    # connect raises the REAL ``psycopg.OperationalError`` (not a pool timeout) straight into
    # the CLI's mapping — reported cleanly and credential-free, exit 1.
    from pydantic import SecretStr
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.postgres import PostgresClient

    settings = PostgresConnectionSettings(
        pg_host="127.0.0.1",
        pg_port=_closed_loopback_port(),
        pg_password=SecretStr("s3cr3t-probe-pw"),
        pg_min_connections=1,
        pg_max_connections=1,
        pg_connect_timeout=2,
    )

    async def _connect(entries: object) -> list[AppliedMigration]:
        async with client_ctx(PostgresClient, settings) as pool, pool.connection():
            pass
        return []

    monkeypatch.setattr(db, "apply_migrations", _connect)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1
    assert "could not connect to Postgres database 'default' at db:5432/tai" in result.output
    # The real connection is refused, not a pool timeout, and the password never surfaces.
    assert "s3cr3t-probe-pw" not in result.output


def test_migrate_clean_on_unconfigured_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(entries: object) -> list[AppliedMigration]:
        raise DatabaseNotConfiguredError("database 'default' is not configured: set TAI_DATABASE_DEFAULT_PG_PASSWORD.")

    monkeypatch.setattr(db, "apply_migrations", _boom)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "is not configured: set TAI_DATABASE_DEFAULT_PG_PASSWORD" in result.output


def _use_real_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # Undo the autouse chain-discovery stub so the real registry runs: a half-set
    # admin identity is raised EAGERLY from discovery (through the skeleton entry's
    # migrator-settings resolve), before any status/apply fake.
    from tai42_skeleton.db import discover_all_migration_chains, discover_plugin_chains, skeleton_entry

    monkeypatch.setattr(db, "discover_all_migration_chains", discover_all_migration_chains)
    monkeypatch.setattr(db, "discover_plugin_chains", discover_plugin_chains)
    monkeypatch.setattr(db, "skeleton_entry", skeleton_entry)
    monkeypatch.delenv("TAI_DB_BINDING_SKELETON", raising=False)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_USER", "migrator")
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD", raising=False)


def test_migrate_clean_on_half_set_admin_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Admin user set, admin password unset: the kit's half-set-admin error escapes
    # discovery and must render as a clean, credential-free failure — never a traceback.
    _use_real_discovery(monkeypatch)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "half-set admin identity" in result.output
    assert "Traceback" not in result.output


def test_status_clean_on_half_set_admin_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_real_discovery(monkeypatch)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "half-set admin identity" in result.output
    assert "Traceback" not in result.output


def test_migrate_loud_on_rewritten_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(entries: object) -> list[AppliedMigration]:
        raise ChecksumMismatchError("component 'skeleton' version 1 ('baseline') was applied with a different checksum")

    monkeypatch.setattr(db, "apply_migrations", _boom)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1
    assert "the chain has been rewritten" in result.output or "different checksum" in result.output


# --- status ---------------------------------------------------------------


def test_status_clean_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,))]

    monkeypatch.setattr(db, "migration_status", _status_fn)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 0, result.output
    assert "up-to-date" in result.output


def test_status_out_of_date_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,), pending=(_script(2, "add_thing"),))]

    monkeypatch.setattr(db, "migration_status", _status_fn)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 1
    assert "OUT OF DATE" in result.output


def test_status_names_skipped_chain_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every component up to date, but a declared chain was skipped: status names it
    # and still exits non-zero, so it stays honest as a CI / pre-deploy gate.
    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,))]

    async def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[SkippedChain(component="acme_alerts")])

    monkeypatch.setattr(db, "migration_status", _status_fn)
    monkeypatch.setattr(db, "discover_all_migration_chains", _discovery)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 1, result.output
    assert "acme_alerts" in result.output
    assert "TAI_DB_BINDING" in result.output


def test_status_json_reports_skipped_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,))]

    async def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[SkippedChain(component="acme_alerts")])

    monkeypatch.setattr(db, "migration_status", _status_fn)
    monkeypatch.setattr(db, "discover_all_migration_chains", _discovery)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "status"])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["status"][0]["component"] == "skeleton"
    assert data["skipped_chains"] == [{"component": "acme_alerts"}]


def test_status_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status("skeleton", applied=(1,))]

    monkeypatch.setattr(db, "migration_status", _status_fn)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "status"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"][0]["component"] == "skeleton"
    assert data["skipped_chains"] == []
