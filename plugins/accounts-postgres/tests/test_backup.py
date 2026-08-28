"""The ``accounts`` backup section: export shape, skip-only restore, version refusal,
and idempotent secret registration — against a scripted cursor (real SQL is the
plugin's e2e concern), mirroring :mod:`tests.test_stores`."""

from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest
from tai42_kit.clients import PostgresConnectionSettings

from tai42_accounts_postgres import backup
from tai42_accounts_postgres.backup import _BackupUserStore, export_accounts, import_accounts

from .conftest import FakeUniqueViolation, ScriptedPg, make_pg_ctx


def _pg(monkeypatch, pg: ScriptedPg) -> None:
    monkeypatch.setattr(backup, "client_ctx", make_pg_ctx(pg))


def _settings() -> PostgresConnectionSettings:
    return PostgresConnectionSettings()


def _row(user_id: str, email: str) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "password_hash": "argon2$hash",
        "role": "member",
        "disabled": False,
        "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    }


async def test_export_users_shape(monkeypatch):
    pg = ScriptedPg(fetches=[[_row("usr-a", "a@x.io"), _row("usr-b", "b@x.io")]])
    _pg(monkeypatch, pg)
    users = await _BackupUserStore(_settings()).export_users()
    assert [u["user_id"] for u in users] == ["usr-a", "usr-b"]
    assert users[0]["password_hash"] == "argon2$hash"  # the verifier round-trips as stored
    assert users[0]["created_at"] == "2026-01-02T03:04:05+00:00"  # JSON-safe iso string
    # settings tier only — sessions/invites are never queried
    assert all("accounts_users" in sql and "accounts_sessions" not in sql for sql, _ in pg.executed)


async def test_export_accounts_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(backup, "accounts_store_configured", lambda: False)
    assert await export_accounts() == {"version": 1, "users": []}


async def test_export_accounts_wraps_version(monkeypatch):
    monkeypatch.setattr(backup, "accounts_store_configured", lambda: True)
    monkeypatch.setattr(backup, "component_store_settings", lambda _c: _settings())
    _pg(monkeypatch, ScriptedPg(fetches=[[_row("usr-a", "a@x.io")]]))
    out = await export_accounts()
    assert out["version"] == 1
    assert len(out["users"]) == 1


async def test_restore_creates_absent_and_skips_existing(monkeypatch):
    # user-a absent (RETURNING yields a row) → created; user-b present (DO NOTHING → None) → skipped.
    pg = ScriptedPg(fetches=[{"user_id": "usr-a"}, None])
    _pg(monkeypatch, pg)
    report = await _BackupUserStore(_settings()).restore_users([_row("usr-a", "a@x.io"), _row("usr-b", "b@x.io")])
    assert report == {"created": 1, "skipped_existing": 1, "errors": []}
    assert all("ON CONFLICT (user_id) DO NOTHING" in sql for sql, _ in pg.executed)


async def test_restore_email_collision_is_a_per_user_error(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("accounts_users_email_unique")])
    _pg(monkeypatch, pg)
    report = await _BackupUserStore(_settings()).restore_users([_row("usr-a", "taken@x.io")])
    assert report["created"] == 0
    assert report["skipped_existing"] == 0
    assert report["errors"] == ["user 'usr-a': accounts_users_email_unique"]


async def test_import_refuses_a_newer_version():
    with pytest.raises(ValueError, match="newer than this plugin supports"):
        await import_accounts({"version": 99, "users": []})
    with pytest.raises(ValueError, match="newer than this plugin supports"):
        await import_accounts({"users": []})  # missing version hits the same guard


async def test_import_refused_when_unconfigured(monkeypatch):
    # A valid-version payload still refuses loudly when the store is not configured.
    monkeypatch.setattr(backup, "accounts_store_configured", lambda: False)
    with pytest.raises(RuntimeError, match="not configured"):
        await import_accounts({"version": 1, "users": [_row("usr-a", "a@x.io")]})


def test_registration_is_idempotent_and_secret(monkeypatch):
    class _FakeBackup:
        def __init__(self):
            self.registered: list[tuple] = []

        def sections(self):
            return [types.SimpleNamespace(name=n) for (n, *_rest) in self.registered]

        def register_section(self, name, exporter, importer, *, secret=False):
            self.registered.append((name, exporter, importer, secret))

    fake = _FakeBackup()
    monkeypatch.setattr(backup, "tai42_app", types.SimpleNamespace(backup=fake))
    backup.register_accounts_backup_section()
    assert fake.registered == [("accounts", export_accounts, import_accounts, True)]
    backup.register_accounts_backup_section()  # a reload must not double-register
    assert len(fake.registered) == 1
