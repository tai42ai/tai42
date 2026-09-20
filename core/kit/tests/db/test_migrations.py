"""The migration runner: file discovery/ordering/checksums, the pinned-connection
apply loop, per-file transactions, the session-scoped advisory lock, checksum
integrity, and multi-DSN grouping.

No real Postgres is opened. ``_pinned_connection`` is the injection seam: a fake
in-process cluster maps each resolved DSN to a fake database (its own history
rows and one shared advisory lock across connections, so concurrent runners
serialise exactly as two sessions would). The live-Postgres replay of real chains
is the e2e harness's job.
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import SecretStr

pytest.importorskip("psycopg")

from psycopg.errors import UndefinedTable

from tai42_kit.clients.settings import PostgresConnectionSettings
from tai42_kit.db import migrations
from tai42_kit.db.migrations import (
    ChecksumMismatchError,
    MigrationDiscoveryError,
    MigrationEntry,
    MigrationError,
    SchemaOutOfDateError,
    apply_migrations,
    assert_chain_applied,
    discover_migrations,
    migration_status,
)

# A migration file whose DDL the fake connection raises on — lets a test drive a
# mid-file failure and assert the per-file transaction rolled the history row back.
_BOOM = "-- BOOM"


# --------------------------------------------------------------------------- #
# Fake Postgres cluster                                                        #
# --------------------------------------------------------------------------- #
class _FakeDb:
    def __init__(self) -> None:
        self.history: list[tuple[str, int, str, str]] = []
        self.applied_ddl: list[str] = []
        self.history_table_created = False
        self.fail_unlock = False
        self.lock = asyncio.Lock()


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple]:
        return self._rows

    async def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeTransaction:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    async def __aenter__(self) -> "_FakeTransaction":
        self._history_len = len(self._db.history)
        self._ddl_len = len(self._db.applied_ddl)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            del self._db.history[self._history_len :]
            del self._db.applied_ddl[self._ddl_len :]
        return False


class _FakeConnection:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db
        self.autocommit = False

    async def set_autocommit(self, value: bool) -> None:
        self.autocommit = value

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._db)

    async def execute(self, sql: str, params: tuple | None = None) -> _FakeCursor:
        text = " ".join(sql.split())
        upper = text.upper()
        if "PG_ADVISORY_LOCK" in upper:
            await self._db.lock.acquire()
            return _FakeCursor([(True,)])
        if "PG_ADVISORY_UNLOCK" in upper:
            if self._db.fail_unlock:
                raise RuntimeError("advisory unlock boom")
            self._db.lock.release()
            return _FakeCursor([(True,)])
        if upper.startswith("CREATE TABLE") and "TAI_SCHEMA_HISTORY" in upper:
            self._db.history_table_created = True
            return _FakeCursor([])
        if upper.startswith("SELECT VERSION, NAME, CHECKSUM FROM TAI_SCHEMA_HISTORY"):
            assert params is not None
            if not self._db.history_table_created:
                # A never-migrated database: the table does not exist. Postgres raises
                # UndefinedTable (SQLSTATE 42P01); the reader must treat it as empty.
                raise UndefinedTable('relation "tai_schema_history" does not exist')
            component = params[0]
            rows = [(v, n, c) for (comp, v, n, c) in self._db.history if comp == component]
            return _FakeCursor(rows)
        if upper.startswith("INSERT INTO TAI_SCHEMA_HISTORY"):
            assert params is not None
            self._db.history.append((params[0], params[1], params[2], params[3]))
            return _FakeCursor([])
        # A migration file's own DDL.
        if text.strip() == _BOOM:
            raise RuntimeError("migration DDL failed")
        self._db.applied_ddl.append(text)
        return _FakeCursor([])


class _FakeCluster:
    def __init__(self) -> None:
        self.dbs: dict[str, _FakeDb] = {}

    def db_for(self, settings: PostgresConnectionSettings) -> _FakeDb:
        return self.dbs.setdefault(settings.pg_dsn, _FakeDb())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = self

        @asynccontextmanager
        async def fake_pinned(settings: PostgresConnectionSettings):
            yield _FakeConnection(cluster.db_for(settings))

        monkeypatch.setattr(migrations, "_pinned_connection", fake_pinned)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _settings(db: str = "db1", host: str = "h1") -> PostgresConnectionSettings:
    return PostgresConnectionSettings(pg_host=host, pg_db=db, pg_password=SecretStr("pw"))


def _write_chain(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / name).write_text(body)
    return root


def _seed(db: _FakeDb, component: str, version: int, name: str, checksum: str) -> None:
    # Seeded history implies the table exists (rows cannot exist without it).
    db.history_table_created = True
    db.history.append((component, version, name, checksum))


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Discovery                                                                    #
# --------------------------------------------------------------------------- #
def test_discover_orders_by_version_and_checksums(tmp_path: Path) -> None:
    root = _write_chain(
        tmp_path / "m",
        {
            "0002_second.sql": "SELECT 2;",
            "0001_first.sql": "SELECT 1;",
            "0010_tenth.sql": "SELECT 10;",
            "notes.txt": "ignored",
        },
    )
    scripts = discover_migrations(root)
    assert [s.version for s in scripts] == [1, 2, 10]
    assert [s.name for s in scripts] == ["first", "second", "tenth"]
    assert scripts[0].checksum == _checksum("SELECT 1;")
    assert scripts[0].sql == "SELECT 1;"


def test_discover_rejects_malformed_filename(tmp_path: Path) -> None:
    root = _write_chain(tmp_path / "m", {"baseline.sql": "SELECT 1;"})
    with pytest.raises(MigrationDiscoveryError, match="NNNN_short_name"):
        discover_migrations(root)


def test_discover_rejects_duplicate_version(tmp_path: Path) -> None:
    root = _write_chain(tmp_path / "m", {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 2;"})
    with pytest.raises(MigrationDiscoveryError, match="duplicate migration version 1"):
        discover_migrations(root)


def test_discover_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(MigrationDiscoveryError, match="not found"):
        discover_migrations(tmp_path / "absent")


# --------------------------------------------------------------------------- #
# Apply                                                                        #
# --------------------------------------------------------------------------- #
async def test_apply_fresh_replays_whole_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();", "0002_second.sql": "CREATE TABLE b();"})

    applied = await apply_migrations([MigrationEntry("skeleton", root, settings)])

    assert [(a.version, a.name) for a in applied] == [(1, "first"), (2, "second")]
    db = cluster.db_for(settings)
    assert db.history_table_created is True
    assert [(row[0], row[1]) for row in db.history] == [("skeleton", 1), ("skeleton", 2)]
    assert db.applied_ddl == ["CREATE TABLE a();", "CREATE TABLE b();"]
    assert db.lock.locked() is False


async def test_apply_runs_only_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    body_1 = "CREATE TABLE a();"
    root = _write_chain(tmp_path / "m", {"0001_first.sql": body_1, "0002_second.sql": "CREATE TABLE b();"})
    _seed(cluster.db_for(settings), "skeleton", 1, "first", _checksum(body_1))

    applied = await apply_migrations([MigrationEntry("skeleton", root, settings)])

    assert [a.version for a in applied] == [2]
    assert cluster.db_for(settings).applied_ddl == ["CREATE TABLE b();"]


async def test_apply_up_to_date_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    body = "CREATE TABLE a();"
    root = _write_chain(tmp_path / "m", {"0001_first.sql": body})
    _seed(cluster.db_for(settings), "skeleton", 1, "first", _checksum(body))

    applied = await apply_migrations([MigrationEntry("skeleton", root, settings)])

    assert applied == []


async def test_apply_checksum_mismatch_is_hard_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();"})
    _seed(cluster.db_for(settings), "skeleton", 1, "first", "deadbeef")

    with pytest.raises(ChecksumMismatchError, match="rewritten"):
        await apply_migrations([MigrationEntry("skeleton", root, settings)])
    # A mismatch aborts before any file runs, and the lock is still released.
    assert cluster.db_for(settings).applied_ddl == []
    assert cluster.db_for(settings).lock.locked() is False


async def test_apply_recorded_version_missing_from_files_is_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();"})
    _seed(cluster.db_for(settings), "skeleton", 5, "gone", "abc123")

    with pytest.raises(ChecksumMismatchError, match="no longer present"):
        await apply_migrations([MigrationEntry("skeleton", root, settings)])


async def test_apply_out_of_order_pending_below_max_is_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    body_1 = "CREATE TABLE a();"
    body_2 = "CREATE TABLE b();"
    body_3 = "CREATE TABLE c();"
    root = _write_chain(
        tmp_path / "m",
        {"0001_first.sql": body_1, "0002_second.sql": body_2, "0003_third.sql": body_3},
    )
    # 0001 and 0003 are applied; a back-inserted 0002 is now pending BELOW the max
    # applied version 3 — applying it would run out of order.
    db = cluster.db_for(settings)
    _seed(db, "skeleton", 1, "first", _checksum(body_1))
    _seed(db, "skeleton", 3, "third", _checksum(body_3))

    with pytest.raises(MigrationError, match="out-of-order"):
        await apply_migrations([MigrationEntry("skeleton", root, settings)])
    # Nothing ran and the lock is released — the refusal is loud, before any DDL.
    assert db.applied_ddl == []
    assert db.lock.locked() is False


async def test_apply_per_file_transaction_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_ok.sql": "CREATE TABLE a();", "0002_boom.sql": _BOOM})

    with pytest.raises(RuntimeError, match="migration DDL failed"):
        await apply_migrations([MigrationEntry("skeleton", root, settings)])

    db = cluster.db_for(settings)
    # 0001 committed; 0002's file failed so its history row rolled back with it.
    assert [row[1] for row in db.history] == [1]
    assert db.lock.locked() is False


# --------------------------------------------------------------------------- #
# Advisory lock + multi-DSN                                                    #
# --------------------------------------------------------------------------- #
async def test_advisory_lock_serialises_concurrent_runners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();", "0002_second.sql": "CREATE TABLE b();"})
    entry = MigrationEntry("skeleton", root, settings)

    first, second = await asyncio.gather(apply_migrations([entry]), apply_migrations([entry]))

    # Exactly one runner applied the chain; the other waited on the lock, then read
    # the now-full history and applied nothing. History has no duplicate rows.
    assert sorted([len(first), len(second)]) == [0, 2]
    db = cluster.db_for(settings)
    assert [row[1] for row in db.history] == [1, 2]


async def test_multi_dsn_gets_separate_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    skeleton_settings = _settings(db="core")
    plugin_settings = _settings(db="plugin_db")
    skeleton_root = _write_chain(tmp_path / "sk", {"0001_base.sql": "CREATE TABLE a();"})
    plugin_root = _write_chain(tmp_path / "pl", {"0001_base.sql": "CREATE TABLE b();"})

    applied = await apply_migrations(
        [
            MigrationEntry("skeleton", skeleton_root, skeleton_settings),
            MigrationEntry("accounts-postgres", plugin_root, plugin_settings),
        ]
    )

    assert {a.component for a in applied} == {"skeleton", "accounts-postgres"}
    assert [row[0] for row in cluster.db_for(skeleton_settings).history] == ["skeleton"]
    assert [row[0] for row in cluster.db_for(plugin_settings).history] == ["accounts-postgres"]
    # Distinct DSNs are distinct databases.
    assert cluster.db_for(skeleton_settings) is not cluster.db_for(plugin_settings)


async def test_same_dsn_components_share_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    skeleton_root = _write_chain(tmp_path / "sk", {"0001_base.sql": "CREATE TABLE a();"})
    plugin_root = _write_chain(tmp_path / "pl", {"0001_base.sql": "CREATE TABLE b();"})

    await apply_migrations(
        [
            MigrationEntry("skeleton", skeleton_root, settings),
            MigrationEntry("accounts-postgres", plugin_root, settings),
        ]
    )

    # One database, one history table, both components recorded.
    assert len(cluster.dbs) == 1
    assert {row[0] for row in cluster.db_for(settings).history} == {"skeleton", "accounts-postgres"}


# --------------------------------------------------------------------------- #
# Status                                                                       #
# --------------------------------------------------------------------------- #
async def test_status_reports_pending_and_up_to_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();", "0002_second.sql": "CREATE TABLE b();"})
    entry = MigrationEntry("skeleton", root, settings)

    before = await migration_status([entry])
    assert before[0].applied_versions == ()
    assert [s.version for s in before[0].pending] == [1, 2]
    assert before[0].is_up_to_date is False

    await apply_migrations([entry])

    after = await migration_status([entry])
    assert after[0].applied_versions == (1, 2)
    assert after[0].pending == ()
    assert after[0].is_up_to_date is True


async def test_status_reports_mismatch_without_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();"})
    _seed(cluster.db_for(settings), "skeleton", 1, "first", "deadbeef")

    status = await migration_status([MigrationEntry("skeleton", root, settings)])

    assert status[0].is_up_to_date is False
    assert [m.version for m in status[0].mismatches] == [1]
    assert status[0].mismatches[0].recorded_checksum == "deadbeef"
    assert status[0].mismatches[0].current_checksum == _checksum("CREATE TABLE a();")


async def test_status_never_creates_history_table_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A never-migrated DB: no history table. ``migration_status`` is READ-ONLY — it
    # issues no CREATE/GRANT (which a lesser-privileged boot-gate role could not run)
    # and reports every component fully pending.
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();", "0002_second.sql": "CREATE TABLE b();"})
    db = cluster.db_for(settings)
    assert db.history_table_created is False

    status = await migration_status([MigrationEntry("skeleton", root, settings)])

    # No DDL of any kind ran: the table was never created and nothing was executed
    # (a GRANT would have landed in ``applied_ddl``), so a role that cannot
    # CREATE/GRANT verifies chain state without crashing.
    assert db.history_table_created is False
    assert db.applied_ddl == []
    # Every migration reports pending, so a boot gate refuses correctly.
    assert status[0].applied_versions == ()
    assert [s.version for s in status[0].pending] == [1, 2]
    assert status[0].is_up_to_date is False


async def test_status_same_component_distinct_dsn_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The skeleton boot gate passes several entries all with component="skeleton",
    # each carrying a DIFFERENT feature DSN. Statuses must map one-per-entry so a
    # stale DSN is never masked by an up-to-date sibling with the same component.
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    up_to_date = _settings(db="applied_db")
    pending = _settings(db="pending_db")
    body = "CREATE TABLE a();"
    root = _write_chain(tmp_path / "m", {"0001_first.sql": body})
    # One DSN is fully migrated; the other has never run.
    _seed(cluster.db_for(up_to_date), "skeleton", 1, "first", _checksum(body))

    statuses = await migration_status(
        [
            MigrationEntry("skeleton", root, up_to_date),
            MigrationEntry("skeleton", root, pending),
        ]
    )

    # Two distinct statuses in input order — the pending one is not overwritten.
    assert len(statuses) == 2
    assert statuses[0].is_up_to_date is True
    assert statuses[1].is_up_to_date is False
    assert [s.version for s in statuses[1].pending] == [1]
    # A boot-gate-style "any not up_to_date" check refuses on the stale second DSN.
    assert any(not status.is_up_to_date for status in statuses)


# --------------------------------------------------------------------------- #
# History table DDL                                                            #
# --------------------------------------------------------------------------- #
async def test_unlock_failure_is_logged_not_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();"})
    cluster.db_for(settings).fail_unlock = True

    with caplog.at_level("WARNING", logger="tai42_kit.db.migrations"):
        applied = await apply_migrations([MigrationEntry("skeleton", root, settings)])

    # The run's outcome survives — the unlock failure is logged, never raised over it.
    assert [a.version for a in applied] == [1]
    assert "advisory unlock failed" in caplog.text


# --------------------------------------------------------------------------- #
# Pinned connection wiring                                                     #
# --------------------------------------------------------------------------- #
async def test_pinned_connection_holds_one_fresh_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    sentinel_conn = object()

    class _FakePool:
        def connection(self):
            @asynccontextmanager
            async def _cm():
                yield sentinel_conn

            return _cm()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, *, fresh, **kwargs):
        captured["client_cls"] = client_cls
        captured["fresh"] = fresh
        captured["kwargs"] = kwargs
        yield _FakePool()

    monkeypatch.setattr(migrations, "client_ctx", fake_client_ctx)

    async with migrations._pinned_connection(_settings()) as conn:
        assert conn is sentinel_conn

    assert captured["client_cls"] is migrations.PostgresClient
    assert captured["fresh"] is True
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    # A dedicated single connection so the session-scoped lock covers the whole run.
    assert kwargs["min_size"] == 1
    assert kwargs["max_size"] == 1
    assert "dsn" in kwargs


def test_history_ddl_defines_table_and_grants_public() -> None:
    ddl = " ".join(migrations._HISTORY_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS tai_schema_history" in ddl
    for column in ("component text", "version integer", "name text", "checksum text", "applied_at timestamptz"):
        assert column in ddl
    assert "PRIMARY KEY (component, version)" in ddl
    assert "GRANT SELECT ON tai_schema_history TO PUBLIC" in ddl


# --------------------------------------------------------------------------- #
# assert_chain_applied — the shared boot-gate primitive                        #
# --------------------------------------------------------------------------- #
async def test_assert_chain_applied_clean_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    body = "CREATE TABLE a();"
    root = _write_chain(tmp_path / "m", {"0001_first.sql": body})
    _seed(cluster.db_for(settings), "skeleton", 1, "first", _checksum(body))

    result = await assert_chain_applied([MigrationEntry("skeleton", root, settings)], remediation="Run the fix.")
    assert result is None


async def test_assert_chain_applied_pending_raises_with_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();"})

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await assert_chain_applied([MigrationEntry("skeleton", root, settings)], remediation="Do XYZ then restart.")
    message = str(excinfo.value)
    # The stale detail names the component and its pending file, and the caller's
    # remediation is appended verbatim — kit hard-codes no CLI verb of its own.
    assert "skeleton: 1 pending (0001_first.sql)" in message
    assert message.endswith("Do XYZ then restart.")
    assert "database schema is out of date — refusing to start" in message


async def test_assert_chain_applied_mismatch_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    settings = _settings()
    root = _write_chain(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a();"})
    _seed(cluster.db_for(settings), "skeleton", 1, "first", "deadbeef")

    with pytest.raises(SchemaOutOfDateError, match="checksum mismatch on version"):
        await assert_chain_applied([MigrationEntry("skeleton", root, settings)], remediation="fix it")
