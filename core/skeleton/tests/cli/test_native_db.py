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
from tai42_skeleton.db import MigrationChainDiscovery

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
    # a real database or scanning the installed distributions.
    monkeypatch.setattr(db, "component_migrator_settings", lambda component: _TARGET)
    monkeypatch.setattr(db, "component_binding", lambda component: "default")

    monkeypatch.setattr(db, "discover_all_migration_chains", lambda: MigrationChainDiscovery(entries=[], skipped=[]))


# --- migrate --------------------------------------------------------------


def test_migrate_applies_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _apply(entries: object) -> list[AppliedMigration]:
        return [AppliedMigration("skeleton", 1, "baseline", "abc")]

    monkeypatch.setattr(db, "apply_migrations", _apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert "Applied skeleton 0001_baseline" in result.output
    assert "Applied 1 migration(s)." in result.output


def test_migrate_applies_every_discovered_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # Migrate discovers every chain first, then applies them all in one run.
    calls: list[str] = []

    async def _apply(entries: object) -> list[AppliedMigration]:
        calls.append("apply:" + ",".join(entry.component for entry in cast("list", entries)))
        return []

    def _discovery() -> MigrationChainDiscovery:
        calls.append("discover")
        entries = cast(
            "list[MigrationEntry]",
            [SimpleNamespace(component=name, settings=_TARGET) for name in ("skeleton", "states", "tai42-widget")],
        )
        return MigrationChainDiscovery(entries=entries, skipped=[])

    monkeypatch.setattr(db, "apply_migrations", _apply)
    monkeypatch.setattr(db, "discover_all_migration_chains", _discovery)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert calls == ["discover", "apply:skeleton,states,tai42-widget"]


def test_migrate_json_reports_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _apply(entries: object) -> list[AppliedMigration]:
        return [AppliedMigration("skeleton", 1, "baseline", "abc")]

    monkeypatch.setattr(db, "apply_migrations", _apply)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "migrate"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == {
        "applied": [{"component": "skeleton", "version": 1, "name": "baseline"}],
        "skipped_chains": [],
        "unlocatable": [],
    }


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
    from tai42_skeleton.db import discover_all_migration_chains

    monkeypatch.setattr(db, "discover_all_migration_chains", discover_all_migration_chains)
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


# --- installed plugin chains ----------------------------------------------


def _widget_spec(**overrides: object):
    """A chain-declaring spec whose package is the installed ``tai42-skeleton`` distribution.

    Its chain resolves against a real installed package (the distribution's own
    ``sql/migrations``) with no live plugin install."""
    from tai42_contract.plugins import PluginSpec

    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": "widget",
            "package": "tai42-skeleton",
            "version": "1.0.0",
            "description": "A test plugin",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1.0",
            "categories": ["dev"],
            "provides": [{"kind": "tool", "name": "w", "module": "w.m", "description": "d"}],
            "migrations": "sql/migrations",
            **overrides,
        }
    )


def _wire_installed_chain(monkeypatch: pytest.MonkeyPatch, installed) -> None:  # type: ignore[no-untyped-def]
    """Real chain discovery over a fixed installed-plugin scan.

    ``installed`` stands in for the scan of the installed distributions, returning the
    located specs (no distribution is unlocatable). The password-only
    database config lets the migrator identity resolve without a connection; only each
    test's PG-touching runner (``migration_status`` / ``apply_migrations``) is stubbed."""
    from tai42_skeleton.db import discover_all_migration_chains
    from tai42_skeleton.db import discovery as db_discovery

    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr(db, "discover_all_migration_chains", discover_all_migration_chains)
    monkeypatch.setattr(db_discovery, "_installed_plugin_specs", lambda: (installed(), []))


def _one_installed_widget(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    import importlib.resources

    root = importlib.resources.files("tai42_skeleton")
    _wire_installed_chain(monkeypatch, lambda: [(_widget_spec(**overrides), root)])


def test_migrate_applies_installed_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # A chain-declaring installed plugin is discovered and applied by migrate, whether or
    # not any manifest loads it.
    _one_installed_widget(monkeypatch)
    applied: list[str] = []

    async def _apply(entries: object) -> list[AppliedMigration]:
        applied.extend(entry.component for entry in cast("list", entries))
        return []

    monkeypatch.setattr(db, "apply_migrations", _apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert applied == ["skeleton", "states", "tai42-skeleton"]


def test_status_gate_nonzero_for_installed_pending_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pending installed chain makes ``tai db status`` exit non-zero and names the
    # component, so a deployment that would boot-gate the chain never passes the
    # pre-deploy gate green.
    _one_installed_widget(monkeypatch)

    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [
            _status(
                entry.component,
                applied=() if entry.component == "tai42-skeleton" else (1,),
                pending=(_script(1, "baseline"),) if entry.component == "tai42-skeleton" else (),
            )
            for entry in cast("list", entries)
        ]

    monkeypatch.setattr(db, "migration_status", _status_fn)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 1, result.output
    assert "tai42-skeleton" in result.output


def test_status_json_lists_installed_component(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _one_installed_widget(monkeypatch)

    async def _status_fn(entries: object) -> list[ComponentStatus]:
        return [_status(entry.component, applied=(1,)) for entry in cast("list", entries)]

    monkeypatch.setattr(db, "migration_status", _status_fn)

    result = CliRunner().invoke(app_module.app, ["--json", "db", "status"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [row["component"] for row in data["status"]] == ["skeleton", "states", "tai42-skeleton"]


def test_status_malformed_installed_spec_is_clean_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    # An installed distribution packaging an invalid plugin spec is a real error, never
    # skipped: discovery raises MigrationDiscoveryError, which the CLI seam maps to a
    # clean, credential-free non-zero, never a false green and never a raw traceback.
    from tai42_kit.db import MigrationDiscoveryError

    def _malformed() -> list:
        raise MigrationDiscoveryError("invalid plugin spec packaged in distribution 'tai42-widget' at /x: bad")

    _wire_installed_chain(monkeypatch, _malformed)

    async def _forbidden_status(entries: object) -> list[ComponentStatus]:
        raise AssertionError("status must not run after a discovery error")

    monkeypatch.setattr(db, "migration_status", _forbidden_status)

    result = CliRunner().invoke(app_module.app, ["db", "status"])

    assert result.exit_code == 1, result.output
    assert "Error:" in result.output
    assert "invalid plugin spec packaged in distribution 'tai42-widget'" in result.output
    assert "Traceback" not in result.output


def test_migrate_malformed_installed_spec_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery runs before any apply, so a malformed installed spec refuses migrate
    # before a single migration lands.
    from tai42_kit.db import MigrationDiscoveryError

    def _malformed() -> list:
        raise MigrationDiscoveryError("invalid plugin spec packaged in distribution 'tai42-widget' at /x: bad")

    _wire_installed_chain(monkeypatch, _malformed)

    async def _forbidden_apply(entries: object) -> list[AppliedMigration]:
        raise AssertionError("migrate must not apply after a discovery error")

    monkeypatch.setattr(db, "apply_migrations", _forbidden_apply)

    result = CliRunner().invoke(app_module.app, ["db", "migrate"])

    assert result.exit_code == 1, result.output
    assert "invalid plugin spec packaged in distribution 'tai42-widget'" in result.output
    assert "Traceback" not in result.output
