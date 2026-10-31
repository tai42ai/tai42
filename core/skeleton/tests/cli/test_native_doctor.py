"""``tai doctor`` — read-only diagnostics.

Covers the exit-code contract (non-zero when any dependency check fails) and the
credential-redaction rule (connection-URL userinfo is masked, never echoed).
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psycopg
import pytest
from click.testing import CliRunner
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_cli import app as app_module
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import AdminIdentityIncompleteError, DatabaseNotConfiguredError

import tai42_skeleton.app.instance as instance
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.cli.native import doctor
from tai42_skeleton.cli.native.doctor import Check
from tai42_skeleton.db import MigrationChainDiscovery
from tai42_skeleton.manifest import Manifest


def test_redact_url_masks_userinfo() -> None:
    assert doctor._redact_url("redis://user:secret@host:6379/0") == "redis://***@host:6379/0"
    assert doctor._redact_url("redis://:secret@host:6379/0") == "redis://***@host:6379/0"
    # No userinfo -> unchanged; unset -> a clear marker, never a blank.
    assert doctor._redact_url("redis://host:6379/0") == "redis://host:6379/0"
    assert doctor._redact_url(None) == "(unset)"


def test_doctor_exits_non_zero_when_a_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _checks() -> list[Check]:
        return [Check("python", doctor._INFO, "3.13.0"), Check("redis", doctor._FAIL, "cannot connect to redis://h")]

    monkeypatch.setattr(doctor, "_run_checks", _checks)

    result = CliRunner().invoke(app_module.app, ["doctor"])

    assert result.exit_code == 1
    assert "redis" in result.output
    assert "fail" in result.output


def test_doctor_exits_zero_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _checks() -> list[Check]:
        return [Check("postgres", doctor._OK, "connected"), Check("redis", doctor._OK, "connected")]

    monkeypatch.setattr(doctor, "_run_checks", _checks)

    result = CliRunner().invoke(app_module.app, ["doctor"])

    assert result.exit_code == 0, result.output


def test_doctor_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    async def _checks() -> list[Check]:
        return [Check("postgres", doctor._OK, "connected")]

    monkeypatch.setattr(doctor, "_run_checks", _checks)

    result = CliRunner().invoke(app_module.app, ["--json", "doctor"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["check"] == "postgres"


def test_probe_redis_fails_and_redacts_password(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_settings = SimpleNamespace(redis=SimpleNamespace(redis_url="redis://:supersecret@cache:6379/0"))
    monkeypatch.setattr(
        "tai42_skeleton.connectors.settings.connector_store_settings",
        lambda: fake_settings,
    )

    @asynccontextmanager
    async def _refusing(*args, **kwargs):
        raise RedisConnectionError("Connection refused")
        yield  # pragma: no cover - unreachable, marks this an async generator

    monkeypatch.setattr(doctor, "client_ctx", _refusing)

    check = asyncio.run(doctor._probe_redis())

    assert check.status == doctor._FAIL
    assert "supersecret" not in check.detail
    assert "***" in check.detail


# -- probe internals ---------------------------------------------------------

# A stand-in for the migrator settings the probes only read for a target
# description; the fake client makes the real connection fields irrelevant.
_TARGET = cast("PostgresConnectionSettings", SimpleNamespace(pg_host="db", pg_port=5432, pg_db="tai"))


class _FakeCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    async def execute(self, *args: object) -> None:
        return None

    async def fetchall(self) -> list[tuple[str]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple[str]] | None = None) -> None:
        self._rows = rows or []

    async def execute(self, *args: object) -> None:
        return None

    @asynccontextmanager
    async def cursor(self):
        yield _FakeCursor(self._rows)


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


def _yielding(pool: _FakePool):
    @asynccontextmanager
    async def factory(*args: object, **kwargs: object):
        yield pool

    return factory


def _refusing():
    import psycopg

    @asynccontextmanager
    async def factory(*args: object, **kwargs: object):
        raise psycopg.OperationalError("connection refused")
        yield  # pragma: no cover - unreachable, marks this an async generator

    return factory


def test_probe_postgres_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "client_ctx", _yielding(_FakePool(_FakeConn())))
    check = asyncio.run(doctor._probe_postgres(_TARGET))
    assert check.status == doctor._OK
    assert "db:5432/tai" in check.detail


def test_probe_postgres_fail_reports_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "client_ctx", _refusing())
    check = asyncio.run(doctor._probe_postgres(_TARGET))
    assert check.status == doctor._FAIL
    assert "cannot connect to db:5432/tai" in check.detail


def _unconfigured():
    @asynccontextmanager
    async def factory(*args: object, **kwargs: object):
        # The kit's named malformed-connection error, raised at DSN/URL construction.
        raise ValueError("the connection is not configured")
        yield  # pragma: no cover - unreachable, marks this an async generator

    return factory


def test_probe_postgres_clean_on_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kit's named not-configured error becomes a clean FAIL check line, not a traceback.
    monkeypatch.setattr(doctor, "client_ctx", _unconfigured())
    check = asyncio.run(doctor._probe_postgres(_TARGET))
    assert check.status == doctor._FAIL
    assert "is not configured" in check.detail


def test_probe_redis_clean_on_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_settings = SimpleNamespace(redis=SimpleNamespace(redis_url=None))
    monkeypatch.setattr(
        "tai42_skeleton.connectors.settings.connector_store_settings",
        lambda: fake_settings,
    )
    monkeypatch.setattr(doctor, "client_ctx", _unconfigured())
    check = asyncio.run(doctor._probe_redis())
    assert check.status == doctor._FAIL
    assert "is not configured" in check.detail


def _component_status(component: str, *, pending: int, mismatch: int):
    from tai42_kit.db import ChecksumMismatch, ComponentStatus, MigrationScript

    scripts = tuple(
        MigrationScript(version=100 + i, name="x", filename=f"{100 + i:04d}_x.sql", checksum="c", sql="")
        for i in range(pending)
    )
    mismatches = tuple(ChecksumMismatch(component, 1, "baseline", "old", "new") for _ in range(mismatch))
    return ComponentStatus(component=component, applied_versions=(1,), pending=scripts, mismatches=mismatches)


def test_probe_schema_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[])

    async def _status(entries: object) -> list:
        return [_component_status("skeleton", pending=0, mismatch=0)]

    monkeypatch.setattr(doctor, "discover_all_migration_chains", _discovery)
    monkeypatch.setattr(doctor, "migration_status", _status)
    check = asyncio.run(doctor._probe_schema(_TARGET))
    assert check.status == doctor._OK
    assert "up to date" in check.detail


def test_probe_schema_out_of_date_names_migrate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[])

    async def _status(entries: object) -> list:
        return [_component_status("skeleton", pending=2, mismatch=0)]

    monkeypatch.setattr(doctor, "discover_all_migration_chains", _discovery)
    monkeypatch.setattr(doctor, "migration_status", _status)
    check = asyncio.run(doctor._probe_schema(_TARGET))
    assert check.status == doctor._FAIL
    assert "tai db migrate" in check.detail
    assert "skeleton" in check.detail


def test_probe_schema_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _discovery() -> MigrationChainDiscovery:
        return MigrationChainDiscovery(entries=[], skipped=[])

    async def _boom(entries: object) -> list:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(doctor, "discover_all_migration_chains", _discovery)
    monkeypatch.setattr(doctor, "migration_status", _boom)
    check = asyncio.run(doctor._probe_schema(_TARGET))
    assert check.status == doctor._FAIL
    assert "cannot inspect schema" in check.detail


def test_probe_schema_bad_chain_is_fail_not_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed installed-plugin chain surfaces as a kit MigrationError from
    # discovery; the probe must render a FAIL naming ``tai db migrate``, not crash.
    from tai42_kit.db import MigrationDiscoveryError

    def _discovery() -> MigrationChainDiscovery:
        raise MigrationDiscoveryError("migration file 'oops.sql' does not match the required form")

    async def _status(entries: object) -> list:  # pragma: no cover - discovery fails first
        raise AssertionError("status must not be reached when discovery fails")

    monkeypatch.setattr(doctor, "discover_all_migration_chains", _discovery)
    monkeypatch.setattr(doctor, "migration_status", _status)
    check = asyncio.run(doctor._probe_schema(_TARGET))
    assert check.status == doctor._FAIL
    assert "tai db migrate" in check.detail
    assert "cannot inspect schema" in check.detail


def test_probe_schema_half_set_admin_is_fail_not_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    # A half-set admin identity escapes discovery; the probe must render a clean FAIL,
    # not crash.
    def _discovery() -> MigrationChainDiscovery:
        raise AdminIdentityIncompleteError(
            "database 'default' has a half-set admin identity: set BOTH "
            "TAI_DATABASE_DEFAULT_PG_ADMIN_USER and TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD, or neither."
        )

    async def _status(entries: object) -> list:  # pragma: no cover - discovery fails first
        raise AssertionError("status must not be reached when discovery fails")

    monkeypatch.setattr(doctor, "discover_all_migration_chains", _discovery)
    monkeypatch.setattr(doctor, "migration_status", _status)
    check = asyncio.run(doctor._probe_schema(_TARGET))
    assert check.status == doctor._FAIL
    assert "half-set admin identity" in check.detail


def test_probe_redis_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_settings = SimpleNamespace(redis=SimpleNamespace(redis_url="redis://cache:6379/0"))
    monkeypatch.setattr(
        "tai42_skeleton.connectors.settings.connector_store_settings",
        lambda: fake_settings,
    )

    class _PingClient:
        async def ping(self) -> bool:
            return True

    @asynccontextmanager
    async def _ok(*args: object, **kwargs: object):
        yield _PingClient()

    monkeypatch.setattr(doctor, "client_ctx", _ok)
    check = asyncio.run(doctor._probe_redis())
    assert check.status == doctor._OK
    assert "connected to redis://cache:6379/0" in check.detail


def test_run_checks_includes_schema_when_postgres_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "component_migrator_settings", lambda component: _TARGET)

    async def _pg(settings: object) -> Check:
        return Check("postgres", doctor._OK, "connected")

    async def _schema(settings: object) -> Check:
        return Check("schema", doctor._OK, "all present")

    async def _redis() -> Check:
        return Check("redis", doctor._OK, "connected")

    monkeypatch.setattr(doctor, "_probe_postgres", _pg)
    monkeypatch.setattr(doctor, "_probe_schema", _schema)
    monkeypatch.setattr(doctor, "_probe_redis", _redis)

    checks = asyncio.run(doctor._run_checks())
    names = [c.name for c in checks]
    assert names == ["python", "config-mode", "postgres", "schema", "redis"]


def test_run_checks_skips_schema_when_postgres_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "component_migrator_settings", lambda component: _TARGET)

    async def _pg(settings: object) -> Check:
        return Check("postgres", doctor._FAIL, "down")

    async def _schema(settings: object) -> Check:  # pragma: no cover - must be skipped
        raise AssertionError("schema probe must be skipped when Postgres is unreachable")

    async def _redis() -> Check:
        return Check("redis", doctor._OK, "connected")

    monkeypatch.setattr(doctor, "_probe_postgres", _pg)
    monkeypatch.setattr(doctor, "_probe_schema", _schema)
    monkeypatch.setattr(doctor, "_probe_redis", _redis)

    checks = asyncio.run(doctor._run_checks())
    names = [c.name for c in checks]
    assert "schema" not in names
    assert names == ["python", "config-mode", "postgres", "redis"]


def test_run_checks_reports_postgres_fail_when_database_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unconfigured(component: str) -> object:
        raise DatabaseNotConfiguredError("database 'default' is not configured: set TAI_DATABASE_DEFAULT_PG_PASSWORD.")

    monkeypatch.setattr(doctor, "component_migrator_settings", _unconfigured)

    async def _schema(settings: object) -> Check:  # pragma: no cover - must be skipped
        raise AssertionError("schema probe must be skipped when the database is not configured")

    async def _redis() -> Check:
        return Check("redis", doctor._OK, "connected")

    monkeypatch.setattr(doctor, "_probe_schema", _schema)
    monkeypatch.setattr(doctor, "_probe_redis", _redis)

    checks = asyncio.run(doctor._run_checks())
    names = [c.name for c in checks]
    assert names == ["python", "config-mode", "postgres", "redis"]
    postgres = next(c for c in checks if c.name == "postgres")
    assert postgres.status == doctor._FAIL
    assert "is not configured" in postgres.detail


def test_run_checks_reports_postgres_fail_when_admin_identity_half_set(monkeypatch: pytest.MonkeyPatch) -> None:
    def _half_set(component: str) -> object:
        raise AdminIdentityIncompleteError(
            "database 'default' has a half-set admin identity: set BOTH "
            "TAI_DATABASE_DEFAULT_PG_ADMIN_USER and TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD, or neither."
        )

    monkeypatch.setattr(doctor, "component_migrator_settings", _half_set)

    async def _schema(settings: object) -> Check:  # pragma: no cover - must be skipped
        raise AssertionError("schema probe must be skipped when the admin identity is half-set")

    async def _redis() -> Check:
        return Check("redis", doctor._OK, "connected")

    monkeypatch.setattr(doctor, "_probe_schema", _schema)
    monkeypatch.setattr(doctor, "_probe_redis", _redis)

    checks = asyncio.run(doctor._run_checks())
    names = [c.name for c in checks]
    assert names == ["python", "config-mode", "postgres", "redis"]
    postgres = next(c for c in checks if c.name == "postgres")
    assert postgres.status == doctor._FAIL
    assert "half-set admin identity" in postgres.detail


def _install_broken_direct_url(monkeypatch: pytest.MonkeyPatch, site: Path) -> None:
    """Put a distribution whose ``direct_url.json`` is not JSON on ``sys.path``."""
    dist_info = site / "tai42_brokenurl-1.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: tai42-brokenurl\nVersion: 1.0.0\n")
    (dist_info / "direct_url.json").write_text("{ not json")
    (dist_info / "RECORD").write_text(f"{dist_info.name}/METADATA,,\n{dist_info.name}/RECORD,,\n")
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()


def _install_nameless_dist_info(monkeypatch: pytest.MonkeyPatch, site: Path) -> None:
    """Put a ``.dist-info`` that holds no ``METADATA`` on ``sys.path``."""
    (site / "stray-1.0.0.dist-info").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()


_BROKEN_METADATA = {
    "direct-url-not-json": (
        _install_broken_direct_url,
        "distribution 'tai42-brokenurl' records a direct_url.json that is not JSON",
    ),
    "dist-info-without-name": (_install_nameless_dist_info, "stray-1.0.0.dist-info records no Name"),
}


# The operator commands that read the installed metadata to report or recover a deployment
# (``tai serve`` and ``tai backend`` read it at boot too); ``{manifest}`` is a manifest file the
# test writes.
_METADATA_DOORS = [
    ["doctor"],
    ["db", "status"],
    ["db", "migrate"],
    ["setup", "--recover", "--token", "t", "--manifest-path", "{manifest}"],
]


@pytest.mark.parametrize("broken", sorted(_BROKEN_METADATA))
@pytest.mark.parametrize("door", _METADATA_DOORS, ids=lambda door: " ".join(door[:2]))
def test_broken_installed_metadata_is_a_clean_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, door: list[str], broken: str
) -> None:
    # Every door reading the installed metadata names the broken file and exits non-zero
    # without a traceback.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    manifest = tmp_path / "manifest.yml"
    manifest.write_text("default_routers: none\n")
    # ``setup --recover`` stamps the path it is given into the environment; set here, the
    # variable is restored when the test ends.
    monkeypatch.setenv("TAI_MANIFEST_PATH", str(manifest))
    argv = [arg.format(manifest=manifest) for arg in door]
    install, expected = _BROKEN_METADATA[broken]
    install(monkeypatch, tmp_path / "site")

    async def _pg(settings: object) -> Check:
        return Check("postgres", doctor._OK, "connected")

    async def _redis() -> Check:
        return Check("redis", doctor._OK, "connected")

    monkeypatch.setattr(doctor, "_probe_postgres", _pg)
    monkeypatch.setattr(doctor, "_probe_redis", _redis)

    # ``setup --recover`` boots the deployment's app up to the installed-metadata read. A
    # fresh app whose boot-manifest read returns the manifest stands in for the process app,
    # so the worker bus and the stored env that read sets up never reach later tests.
    server = TaiMCP(name="metadata-door-under-test")

    async def _read_boot_manifest(*, kind: object) -> Manifest:
        return Manifest.model_validate({"default_routers": "none"})

    monkeypatch.setattr(server, "_read_boot_manifest", _read_boot_manifest)
    monkeypatch.setattr(instance, "app", server)

    result = CliRunner().invoke(app_module.app, argv)

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert expected in result.output
