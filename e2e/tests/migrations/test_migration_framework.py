"""The migration framework, exercised against a live Postgres.

Three legs cover the runner's behaviour on the model the product ships on:

- *fresh-install replay*: applying the product chains (skeleton + the
  accounts-postgres plugin) to a clean database applies every baseline, records
  it in ``tai_schema_history``, and yields the same ``public``-schema table-NAME
  set the session template DB holds — a from-scratch migrate produces the template's
  table set (the leg asserts table-name equality, not a byte-for-byte schema diff).
- *upgrade*: a component at 0001 gains a synthetic 0002; the runner applies only
  the new file, records both versions, and the boot-gate verdict is then clean.
- *tamper*: rewriting an already-applied file's bytes makes ``apply`` a hard
  ``ChecksumMismatchError`` and ``migration_status`` report the mismatch (the data
  a boot gate turns into a loud refusal) — a rewritten chain is never re-run.

These need only the shared Postgres, not a booted stack: each leg owns a scratch
database it drops at teardown.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import ChecksumMismatchError, MigrationEntry, apply_migrations, migration_status

from tai42_e2e.pg import _TEMPLATE_DB, PostgresAdmin, product_migration_entries
from tai42_e2e.settings import HarnessSettings

_UPGRADE_COMPONENT = "e2e-upgrade-probe"
_TAMPER_COMPONENT = "e2e-tamper-probe"


def _runner_settings(harness: HarnessSettings, dbname: str) -> PostgresConnectionSettings:
    """DDL-privileged connection settings for the runner, pointed at ``dbname`` on
    the shared Postgres — the same explicit per-entry settings shape the harness
    template build uses."""
    return PostgresConnectionSettings(
        pg_host=harness.pg_host,
        pg_port=harness.pg_port,
        pg_db=dbname,
        pg_user=harness.pg_user,
        pg_password=SecretStr(harness.pg_password),
    )


def _public_tables(harness: HarnessSettings, dbname: str) -> set[str]:
    """The ``public``-schema table set of ``dbname`` (the migration history table
    excluded, so two databases are compared on their migrated schema alone)."""
    with (
        psycopg.connect(
            host=harness.pg_host,
            port=harness.pg_port,
            user=harness.pg_user,
            password=harness.pg_password,
            dbname=dbname,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {row[0] for row in cur.fetchall()} - {"tai_schema_history"}


def _history(harness: HarnessSettings, dbname: str, component: str) -> dict[int, str]:
    """The recorded ``version -> checksum`` map for ``component`` in ``dbname``."""
    with (
        psycopg.connect(
            host=harness.pg_host,
            port=harness.pg_port,
            user=harness.pg_user,
            password=harness.pg_password,
            dbname=dbname,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT version, checksum FROM tai_schema_history WHERE component = %s ORDER BY version",
            (component,),
        )
        return {int(version): checksum for version, checksum in cur.fetchall()}


@pytest.fixture(scope="module")
def harness_settings() -> HarnessSettings:
    return HarnessSettings()


@pytest.fixture(scope="module")
def pg_admin(harness_settings: HarnessSettings) -> PostgresAdmin:
    admin = PostgresAdmin(harness_settings)
    admin.check_reachable()
    return admin


@pytest.fixture
def scratch_db(pg_admin: PostgresAdmin) -> Iterator[str]:
    """A fresh, empty database dropped at teardown."""
    name = f"tai42_e2e_mig_{secrets.token_hex(4)}"
    pg_admin.create_empty_db(name)
    try:
        yield name
    finally:
        pg_admin.drop_stack_db(name)


def test_fresh_install_replay_matches_template(
    harness_settings: HarnessSettings, pg_admin: PostgresAdmin, scratch_db: str
) -> None:
    # The template DB is built by the same product chains; rebuild it here so the
    # comparison never depends on which stack module ran first, then replay the
    # product chains onto a clean database and assert the two schemas match.
    pg_admin.ensure_template()

    settings = _runner_settings(harness_settings, scratch_db)
    applied = asyncio.run(apply_migrations(product_migration_entries(settings)))

    by_component = {(a.component, a.version) for a in applied}
    assert ("skeleton", 1) in by_component
    assert ("tai42-accounts-postgres", 1) in by_component

    # Both baselines are recorded in the fresh database's history table.
    assert 1 in _history(harness_settings, scratch_db, "skeleton")
    assert 1 in _history(harness_settings, scratch_db, "tai42-accounts-postgres")

    # A from-scratch migrate yields the same ``public`` table-name set as the
    # template (a table-name-set comparison, not a byte-for-byte schema diff).
    assert _public_tables(harness_settings, scratch_db) == _public_tables(harness_settings, _TEMPLATE_DB)
    # The accounts plugin's own tables are present (the plugin chain really ran).
    assert {"accounts_users", "accounts_sessions", "accounts_invites"} <= _public_tables(harness_settings, scratch_db)

    # Re-applying the fully-migrated chain is an inert no-op, and the boot-gate
    # verdict is clean.
    assert asyncio.run(apply_migrations(product_migration_entries(settings))) == []
    statuses = asyncio.run(migration_status(product_migration_entries(settings)))
    assert all(status.is_up_to_date for status in statuses)


def test_upgrade_applies_synthetic_0002_and_records_both(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    chain = tmp_path / "upgrade_chain"
    chain.mkdir()
    (chain / "0001_probe.sql").write_text("CREATE TABLE mig_probe (id integer PRIMARY KEY);\n")
    settings = _runner_settings(harness_settings, scratch_db)
    entry = MigrationEntry(component=_UPGRADE_COMPONENT, migrations_dir=chain, settings=settings)

    first = asyncio.run(apply_migrations([entry]))
    assert [(a.component, a.version) for a in first] == [(_UPGRADE_COMPONENT, 1)]
    assert set(_history(harness_settings, scratch_db, _UPGRADE_COMPONENT)) == {1}

    # A synthetic 0002 lands in the chain; only it is pending and applied.
    (chain / "0002_probe_note.sql").write_text("ALTER TABLE mig_probe ADD COLUMN note text;\n")
    second = asyncio.run(apply_migrations([entry]))
    assert [(a.component, a.version) for a in second] == [(_UPGRADE_COMPONENT, 2)]

    # Both versions are now recorded, and 0002's effect is live.
    assert set(_history(harness_settings, scratch_db, _UPGRADE_COMPONENT)) == {1, 2}
    with (
        psycopg.connect(
            host=harness_settings.pg_host,
            port=harness_settings.pg_port,
            user=harness_settings.pg_user,
            password=harness_settings.pg_password,
            dbname=scratch_db,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'mig_probe'"
        )
        assert {row[0] for row in cur.fetchall()} == {"id", "note"}

    # The boot-gate verdict for the upgraded chain is clean (nothing pending).
    [status] = asyncio.run(migration_status([entry]))
    assert status.is_up_to_date
    assert status.applied_versions == (1, 2)


def test_tampered_checksum_is_a_hard_error(harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path) -> None:
    chain = tmp_path / "tamper_chain"
    chain.mkdir()
    script = chain / "0001_probe.sql"
    script.write_text("CREATE TABLE tamper_probe (id integer PRIMARY KEY);\n")
    settings = _runner_settings(harness_settings, scratch_db)
    entry = MigrationEntry(component=_TAMPER_COMPONENT, migrations_dir=chain, settings=settings)

    asyncio.run(apply_migrations([entry]))
    recorded = _history(harness_settings, scratch_db, _TAMPER_COMPONENT)
    assert set(recorded) == {1}

    # Rewrite the already-applied file's bytes: the recorded checksum no longer
    # matches, so the chain has been rewritten.
    script.write_text("CREATE TABLE tamper_probe (id integer PRIMARY KEY); -- edited after apply\n")

    with pytest.raises(ChecksumMismatchError):
        asyncio.run(apply_migrations([entry]))

    # ``migration_status`` never raises — it reports the mismatch as data (what a
    # boot gate renders into its loud refusal), and the run applied nothing new.
    [status] = asyncio.run(migration_status([entry]))
    assert not status.is_up_to_date
    assert [mismatch.version for mismatch in status.mismatches] == [1]
    assert _history(harness_settings, scratch_db, _TAMPER_COMPONENT) == recorded
