"""The Postgres seam's control flow against a scripted psycopg cursor.

Exercises the SQL-issuing paths in ``stores.py`` (the login-row insert and its
constraint-name split, atomic invite consume) without a live database. Real SQL correctness is proven by the e2e leg.
"""

from __future__ import annotations

import pytest
from tai42_kit.clients import PostgresConnectionSettings

from tai42_accounts_postgres import stores
from tai42_accounts_postgres.stores import (
    EmailTakenError,
    InvitesStore,
    LoginExistsError,
    SessionsStore,
    UsersStore,
    new_user_id,
)

from .conftest import FakeUniqueViolation, ScriptedPg, future, make_pg_ctx, past


def _pg(monkeypatch, pg: ScriptedPg) -> None:
    monkeypatch.setattr(stores, "client_ctx", make_pg_ctx(pg))


def _settings() -> PostgresConnectionSettings:
    # The scripted ``client_ctx`` fake never opens a connection, so a bare settings
    # object is enough for the stores' control-flow tests.
    return PostgresConnectionSettings()


def test_new_user_id_is_prefixed():
    assert new_user_id().startswith("usr-")


async def test_create_login_inserts(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    await UsersStore(_settings()).create_login("usr-1", "a@b.c")
    assert any("INSERT INTO accounts_users" in sql for sql, _ in pg.executed)


async def test_create_login_writes_email_and_hash(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    await UsersStore(_settings()).create_login("usr-1", "a@b.c", "argon2$x")
    sql, params = next((s, p) for s, p in pg.executed if "INSERT INTO accounts_users" in s)
    assert sql == "INSERT INTO accounts_users (user_id, email, password_hash) VALUES (%s, %s, %s)"
    assert params == ("usr-1", "a@b.c", "argon2$x")


async def test_create_login_null_password_when_unset(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    await UsersStore(_settings()).create_login("usr-1", "a@b.c")
    _sql, params = next((s, p) for s, p in pg.executed if "INSERT INTO accounts_users" in s)
    assert params == ("usr-1", "a@b.c", None)


async def test_create_login_email_taken_raises_typed(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("accounts_users_email_unique")])
    _pg(monkeypatch, pg)
    with pytest.raises(EmailTakenError):
        await UsersStore(_settings()).create_login("usr-1", "a@b.c")


async def test_create_login_user_id_collision_raises_login_exists(monkeypatch):
    # The principal owns the id, so a collision is a loud invariant breach — never a
    # silent regeneration.
    pg = ScriptedPg(errors=[FakeUniqueViolation("accounts_users_user_id_unique")])
    _pg(monkeypatch, pg)
    with pytest.raises(LoginExistsError):
        await UsersStore(_settings()).create_login("usr-1", "a@b.c")


async def test_create_login_unexpected_unique_reraises(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("some_other_constraint")])
    _pg(monkeypatch, pg)
    with pytest.raises(FakeUniqueViolation):
        await UsersStore(_settings()).create_login("usr-1", "a@b.c")


async def test_get_by_email_and_user_id(monkeypatch):
    row = {"user_id": "usr-1", "email": "a@b", "password_hash": None, "disabled": False}
    pg = ScriptedPg(fetches=[row, None])
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    assert await store.get_by_email("a@b") == row
    assert await store.get_by_user_id("missing") is None


async def test_list_returns_rows(monkeypatch):
    rows = [
        {
            "user_id": "usr-1",
            "email": "a@b",
            "disabled": False,
            "created_at": future(0),
            "pending_invite": True,
        }
    ]
    pg = ScriptedPg(fetches=[rows])
    _pg(monkeypatch, pg)
    assert await UsersStore(_settings()).list() == rows


async def test_mutations_execute(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    await store.set_password_hash("usr-1", "h")
    await store.set_disabled("usr-1", True)
    await store.delete("usr-1")
    kinds = [sql.split()[0] for sql, _ in pg.executed]
    assert kinds == ["UPDATE", "UPDATE", "DELETE"]


async def test_sessions_create_sweeps_then_inserts(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    await SessionsStore(_settings()).create("th", "usr-1", future())
    sqls = [sql for sql, _ in pg.executed]
    assert any("DELETE FROM accounts_sessions WHERE absolute_expires_at" in s for s in sqls)
    assert any("INSERT INTO accounts_sessions" in s for s in sqls)


async def test_sessions_resolve_and_touch(monkeypatch):
    row = {
        "user_id": "usr-1",
        "email": "a@b",
        "disabled": False,
        "last_seen_at": past(30),
        "absolute_expires_at": future(),
    }
    pg = ScriptedPg(fetches=[row])
    _pg(monkeypatch, pg)
    store = SessionsStore(_settings())
    assert await store.resolve("th") == row
    await store.touch("th", future(0))
    assert any("UPDATE accounts_sessions SET last_seen_at" in sql for sql, _ in pg.executed)


async def test_sessions_delete_rowcount(monkeypatch):
    _pg(monkeypatch, ScriptedPg(rowcount=1))
    assert await SessionsStore(_settings()).delete("th") is True
    _pg(monkeypatch, ScriptedPg(rowcount=0))
    assert await SessionsStore(_settings()).delete("th") is False


async def test_sessions_delete_for_user_keep_variants(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    store = SessionsStore(_settings())
    await store.delete_for_user("usr-1")
    await store.delete_for_user("usr-1", keep_token_hash="keep")
    sqls = [sql for sql, _ in pg.executed]
    assert "token_hash <>" not in sqls[0]
    assert "token_hash <>" in sqls[1]


async def test_invites_create_replaces_and_sweeps(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    await InvitesStore(_settings()).create("th", "usr-1", future())
    sqls = [sql for sql, _ in pg.executed]
    assert any("DELETE FROM accounts_invites WHERE user_id" in s for s in sqls)
    assert any("consumed_at IS NOT NULL OR expires_at" in s for s in sqls)
    assert any("INSERT INTO accounts_invites" in s for s in sqls)


async def test_invites_consume_hit_and_miss(monkeypatch):
    _pg(monkeypatch, ScriptedPg(fetches=[{"user_id": "usr-1"}]))
    assert await InvitesStore(_settings()).consume("th", future(0)) == "usr-1"
    _pg(monkeypatch, ScriptedPg(fetches=[None]))
    assert await InvitesStore(_settings()).consume("th", future(0)) is None


async def test_invites_delete_for_user(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    await InvitesStore(_settings()).delete_for_user("usr-1")
    assert any("DELETE FROM accounts_invites WHERE user_id" in sql for sql, _ in pg.executed)
