"""The packaged migration chain, its schema shape, and the boot-time gate.

The baseline is loaded through the kit runner's own discovery (the same path
production uses) and asserted schema-equal to the accounts store the plugin
reads and writes: the three tables, their unique constraints, and the two
per-user indexes — with no ``ALTER ... ADD COLUMN`` backfill (greenfield: the
baseline IS the schema). The gate is exercised with a faked ``migration_status``
so the refusal RENDERING and WIRING are pinned without a live Postgres.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import pytest
import yaml
from tai42_kit.db import ChecksumMismatch, ComponentStatus, MigrationEntry, MigrationScript, discover_migrations

from tai42_accounts_postgres.db import (
    COMPONENT,
    SchemaOutOfDateError,
    accounts_migration_entry,
    accounts_store_configured,
    assert_accounts_schema_applied,
)


def _baseline_sql() -> str:
    scripts = discover_migrations(accounts_migration_entry().migrations_dir)
    assert scripts, "the accounts chain must ship at least the baseline migration"
    return scripts[0].sql


def _table_block(ddl: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", ddl, re.DOTALL)
    assert match is not None, f"{table} table not found in the baseline"
    return match.group(1)


# -- chain discovery ------------------------------------------------------------


def test_chain_is_the_single_baseline_migration():
    scripts = discover_migrations(accounts_migration_entry().migrations_dir)
    assert [s.filename for s in scripts] == ["0001_baseline.sql"]
    assert scripts[0].version == 1
    assert scripts[0].name == "baseline"


def test_migration_entry_identity_is_the_distribution_name():
    entry = accounts_migration_entry()
    # The history-table identity MUST equal the distribution name so the boot gate
    # reads the same rows the installer and the `tai db` CLI write.
    assert entry.component == COMPONENT == "tai42-accounts-postgres"


def test_tai_plugin_yml_declares_the_migrations_dir():
    from pathlib import Path

    spec = yaml.safe_load((Path(__file__).resolve().parent.parent / "tai-plugin.yml").read_text(encoding="utf-8"))
    # A bare, package-relative POSIX dir with no trailing slash — the runner
    # resolves it against the import root, which is where the chain is packaged.
    assert spec["migrations"] == "migrations"
    discovered = discover_migrations(accounts_migration_entry().migrations_dir)
    assert [s.filename for s in discovered] == ["0001_baseline.sql"]


# -- baseline schema equality ---------------------------------------------------


def test_baseline_creates_the_three_owned_tables():
    ddl = _baseline_sql()
    assert "CREATE TABLE IF NOT EXISTS accounts_users" in ddl
    assert "CREATE TABLE IF NOT EXISTS accounts_sessions" in ddl
    assert "CREATE TABLE IF NOT EXISTS accounts_invites" in ddl


def test_users_table_keys_and_columns():
    block = _table_block(_baseline_sql(), "accounts_users")
    for column in ("user_id", "email", "password_hash", "role", "disabled", "created_at"):
        assert re.search(rf"\b{column}\b", block) is not None, f"{column} missing from accounts_users"
    # password_hash is nullable (NULL until an invite is accepted); the tri-state
    # cannot be a NOT NULL column.
    hash_line = next(line for line in block.splitlines() if line.strip().startswith("password_hash"))
    assert "NOT NULL" not in hash_line
    assert "CONSTRAINT accounts_users_user_id_unique UNIQUE (user_id)" in block
    assert "CONSTRAINT accounts_users_email_unique UNIQUE (email)" in block


def test_sessions_table_hash_uniqueness_and_index():
    ddl = _baseline_sql()
    block = _table_block(ddl, "accounts_sessions")
    assert "CONSTRAINT accounts_sessions_token_hash_unique UNIQUE (token_hash)" in block
    assert "absolute_expires_at TIMESTAMPTZ NOT NULL" in block
    assert "CREATE INDEX IF NOT EXISTS accounts_sessions_user_id_idx ON accounts_sessions (user_id)" in ddl


def test_invites_table_hash_uniqueness_and_index():
    ddl = _baseline_sql()
    block = _table_block(ddl, "accounts_invites")
    assert "CONSTRAINT accounts_invites_token_hash_unique UNIQUE (token_hash)" in block
    assert "consumed_at TIMESTAMPTZ" in block
    assert "CREATE INDEX IF NOT EXISTS accounts_invites_user_id_idx ON accounts_invites (user_id)" in ddl


def test_baseline_folds_schema_into_create_table_with_no_alter_backfill():
    # Greenfield migration model: the baseline IS the schema — no in-place upgrade
    # ALTER survives into it. A re-introduced backfill fails here without a live DB.
    assert "ADD COLUMN" not in _baseline_sql()


# -- store-configured predicate -------------------------------------------------


def test_store_configured_true_when_password_resolves(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    assert accounts_store_configured() is True


def test_store_configured_false_without_any_password(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    assert accounts_store_configured() is False


def test_store_configured_follows_component_binding(monkeypatch: pytest.MonkeyPatch):
    # The predicate resolves the component's binding, not the "default" database
    # directly: bound to a named database, it reads that database's password.
    monkeypatch.setenv("TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES", "accounts")
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    monkeypatch.setenv("TAI_DATABASE_ACCOUNTS_PG_PASSWORD", "secret")
    assert accounts_store_configured() is True


# -- boot gate ------------------------------------------------------------------


def _script(version: int, name: str) -> MigrationScript:
    return MigrationScript(version=version, name=name, filename=f"{version:04d}_{name}.sql", checksum="c", sql="")


def _status(*, pending: tuple = (), mismatches: tuple = ()) -> ComponentStatus:
    return ComponentStatus(component=COMPONENT, applied_versions=(1,), pending=pending, mismatches=mismatches)


def _fake_status(result: list[ComponentStatus]):
    async def _fn(entries: Sequence[MigrationEntry]) -> list[ComponentStatus]:
        return result

    return _fn


async def test_gate_clean_is_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    # The gate is the shared kit primitive; fake kit's ``migration_status`` at its
    # source so the real ``assert_chain_applied`` renders the refusal end-to-end.
    monkeypatch.setattr("tai42_kit.db.migrations.migration_status", _fake_status([_status()]))
    await assert_accounts_schema_applied()  # no raise


async def test_gate_pending_refuses_naming_migrate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    monkeypatch.setattr(
        "tai42_kit.db.migrations.migration_status", _fake_status([_status(pending=(_script(2, "add_thing"),))])
    )
    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await assert_accounts_schema_applied()
    message = str(excinfo.value)
    assert "tai db migrate" in message
    assert "tai42-accounts-postgres" in message
    assert "0002_add_thing.sql" in message


async def test_gate_checksum_mismatch_refuses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    mismatch = ChecksumMismatch(COMPONENT, 1, "baseline", "old", "new")
    monkeypatch.setattr("tai42_kit.db.migrations.migration_status", _fake_status([_status(mismatches=(mismatch,))]))
    with pytest.raises(SchemaOutOfDateError) as excinfo:
        await assert_accounts_schema_applied()
    assert "checksum mismatch" in str(excinfo.value)
    assert "tai db migrate" in str(excinfo.value)


async def test_gate_skipped_when_store_unconfigured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    async def _forbidden(entries: Sequence[MigrationEntry]) -> list[ComponentStatus]:
        raise AssertionError("an unconfigured accounts store must not query the database")

    monkeypatch.setattr("tai42_kit.db.migrations.migration_status", _forbidden)
    await assert_accounts_schema_applied()  # no query, no raise
