"""The Postgres seam's control flow against a scripted psycopg cursor.

Exercises the SQL-issuing paths in ``stores.py`` (id-collision retry,
constraint-name split, advisory-locked bootstrap insert, atomic invite consume)
without a live database. Real SQL correctness is proven by the e2e leg.
"""

from __future__ import annotations

import pytest

from tai42_accounts_postgres import stores
from tai42_accounts_postgres.settings import AccountsPgSettings
from tai42_accounts_postgres.stores import (
    EmailTakenError,
    InvitesStore,
    SessionsStore,
    UsersStore,
    new_user_id,
)

from .conftest import FakeUniqueViolation, ScriptedPg, future, make_pg_ctx, past


def _pg(monkeypatch, pg: ScriptedPg) -> None:
    monkeypatch.setattr(stores, "client_ctx", make_pg_ctx(pg))


def _settings() -> AccountsPgSettings:
    return AccountsPgSettings()


def test_new_user_id_is_prefixed():
    assert new_user_id().startswith("usr-")


async def test_create_inserts_and_returns_id(monkeypatch):
    pg = ScriptedPg()
    _pg(monkeypatch, pg)
    result = await UsersStore(_settings()).create("usr-1", "a@b.c", "admin")
    assert result == "usr-1"
    assert any("INSERT INTO accounts_users" in sql for sql, _ in pg.executed)


async def test_create_email_taken_raises_typed(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("accounts_users_email_unique")])
    _pg(monkeypatch, pg)
    with pytest.raises(EmailTakenError):
        await UsersStore(_settings()).create("usr-1", "a@b.c", "admin")


async def test_create_user_id_collision_regenerates_then_succeeds(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("accounts_users_user_id_unique"), None])
    _pg(monkeypatch, pg)
    result = await UsersStore(_settings()).create("usr-seed", "a@b.c", "admin")
    # The second attempt used a freshly generated id, not the colliding seed.
    assert result != "usr-seed"
    assert len([sql for sql, _ in pg.executed if "INSERT INTO accounts_users" in sql]) == 2


async def test_create_user_id_collision_exhausts_and_raises(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("accounts_users_user_id_unique")] * 3)
    _pg(monkeypatch, pg)
    with pytest.raises(RuntimeError, match="accounts_users_user_id_unique"):
        await UsersStore(_settings()).create("usr-seed", "a@b.c", "admin")


async def test_create_unexpected_unique_reraises(monkeypatch):
    pg = ScriptedPg(errors=[FakeUniqueViolation("some_other_constraint")])
    _pg(monkeypatch, pg)
    with pytest.raises(FakeUniqueViolation):
        await UsersStore(_settings()).create("usr-1", "a@b.c", "admin")


async def test_create_owner_if_first_inserts_when_empty(monkeypatch):
    row = {"user_id": "usr-1", "email": "o@x", "role": "admin", "disabled": False, "created_at": future(0)}
    pg = ScriptedPg(fetches=[{"n": 0}, row])
    _pg(monkeypatch, pg)
    result = await UsersStore(_settings()).create_owner_if_first("usr-1", "o@x", "hash", "admin")
    assert result == row
    assert any("pg_advisory_xact_lock" in sql for sql, _ in pg.executed)


async def test_create_owner_if_first_returns_none_when_users_exist(monkeypatch):
    pg = ScriptedPg(fetches=[{"n": 1}])
    _pg(monkeypatch, pg)
    assert await UsersStore(_settings()).create_owner_if_first("usr-1", "o@x", "hash", "admin") is None


async def test_get_by_email_and_user_id(monkeypatch):
    row = {"user_id": "usr-1", "email": "a@b", "password_hash": None, "role": "admin", "disabled": False}
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
            "role": "admin",
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
    await store.set_role("usr-1", "viewer")
    await store.set_disabled("usr-1", True)
    await store.delete("usr-1")
    kinds = [sql.split()[0] for sql, _ in pg.executed]
    assert kinds == ["UPDATE", "UPDATE", "UPDATE", "DELETE"]


async def test_count_and_admin_count(monkeypatch):
    pg = ScriptedPg(fetches=[{"n": 3}, {"n": 0}, None])
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    assert await store.count() == 3
    assert await store.count_other_enabled_admins("usr-1") == 0
    assert await store.count() == 0  # None row -> 0


async def test_admin_guard_txn_locks_counts_and_mutates(monkeypatch):
    # The advisory lock, the last-admin count, and its authorized mutation all run
    # on one connection/transaction — the atomic guard against concurrent removals.
    pg = ScriptedPg(fetches=[{"n": 1}])
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    async with store.admin_guard_txn() as guard:
        assert await guard.count_other_enabled_admins("usr-1") == 1
        await guard.set_disabled("usr-1", True)
        await guard.set_role("usr-1", "viewer")
        await guard.delete("usr-1")
    sqls = [sql for sql, _ in pg.executed]
    assert any("pg_advisory_xact_lock" in s for s in sqls)
    assert any("UPDATE accounts_users SET disabled" in s for s in sqls)
    assert any("UPDATE accounts_users SET role" in s for s in sqls)
    assert any("DELETE FROM accounts_users WHERE user_id" in s for s in sqls)


async def test_admin_guard_txn_reads_target_and_cleans_credentials(monkeypatch):
    # read_target re-reads the locked role/disabled; the credential cleanup runs on
    # the same guard cursor (sessions + invites in this transaction).
    pg = ScriptedPg(fetches=[{"role": "admin", "disabled": False}])
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    async with store.admin_guard_txn() as guard:
        assert await guard.read_target("usr-1") == {"role": "admin", "disabled": False}
        await guard.delete_sessions_for_user("usr-1")
        await guard.delete_invites_for_user("usr-1")
    sqls = [sql for sql, _ in pg.executed]
    assert any("SELECT role, disabled FROM accounts_users WHERE user_id" in s for s in sqls)
    assert any("DELETE FROM accounts_sessions WHERE user_id" in s for s in sqls)
    assert any("DELETE FROM accounts_invites WHERE user_id" in s for s in sqls)


async def test_admin_guard_txn_read_target_missing_is_none(monkeypatch):
    pg = ScriptedPg(fetches=[None])
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    async with store.admin_guard_txn() as guard:
        assert await guard.read_target("ghost") is None


async def test_admin_guard_txn_count_none_row_is_zero(monkeypatch):
    pg = ScriptedPg(fetches=[None])
    _pg(monkeypatch, pg)
    store = UsersStore(_settings())
    async with store.admin_guard_txn() as guard:
        assert await guard.count_other_enabled_admins("usr-1") == 0


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
        "role": "admin",
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
