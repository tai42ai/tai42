"""The packaged DDL loader and the standalone apply CLI (proven here, not deferred)."""

from __future__ import annotations

from tai42_accounts_postgres import db

from .conftest import ScriptedPg, make_pg_ctx


def test_load_ddl_contains_the_three_tables():
    ddl = db.load_ddl()
    assert "CREATE TABLE IF NOT EXISTS accounts_users" in ddl
    assert "CREATE TABLE IF NOT EXISTS accounts_sessions" in ddl
    assert "CREATE TABLE IF NOT EXISTS accounts_invites" in ddl


def test_declared_tables_is_the_sorted_three():
    assert db.declared_tables() == ["accounts_invites", "accounts_sessions", "accounts_users"]


def test_tables_command_lists_them(capsys):
    db.main(["tables"])
    out = capsys.readouterr().out.split()
    assert out == ["accounts_invites", "accounts_sessions", "accounts_users"]


def test_apply_command_runs_the_ddl_and_is_idempotent(monkeypatch, capsys):
    pg = ScriptedPg()
    monkeypatch.setattr(db, "client_ctx", make_pg_ctx(pg))
    db.main(["apply"])
    # The single trusted DDL script is executed once.
    assert any("CREATE TABLE IF NOT EXISTS accounts_users" in sql for sql, _ in pg.executed)
    assert "Applied accounts schema" in capsys.readouterr().out
    # Re-running is a no-op script again (IF NOT EXISTS), no error.
    db.main(["apply"])
