"""A REAL Postgres exercise of the account stores: the constraint-name split on a
live unique violation, the advisory-locked last-admin guard under two concurrent
callers, opportunistic session-expiry sweeping, session revocation, and atomic
single-use/TTL invite consumption.

These need real Postgres semantics (named unique constraints, ``pg_advisory_xact_lock``
serialization, ``now()``) that the scripted ``FakeCursor`` in :mod:`tests.conftest` can
only imitate. It is OPT-IN: set ``TAI42_ACCOUNTS_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres. Without the opt-in the tests SKIP
VISIBLY with a clear reason (never a silent skip)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tai42_kit.clients import PostgresConnectionSettings

from tai42_accounts_postgres.stores import (
    EmailTakenError,
    InvitesStore,
    LoginExistsError,
    SessionsStore,
    UsersStore,
    new_user_id,
)

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _future(seconds: int = 3600) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _past(seconds: int = 3600) -> datetime:
    return _now() - timedelta(seconds=seconds)


async def _make_user(store: UsersStore, email: str, role: str, password_hash: str | None = None) -> str:
    """Create a login row for a fresh principal id and return that id."""
    uid = new_user_id()
    await store.create_login(uid, email, role, password_hash)
    return uid


async def test_provider_has_login_tracks_the_row(accounts_db: PostgresConnectionSettings) -> None:
    # ``has_login`` is the ownership signal the principals door reads: True once a login row
    # exists for the principal, False otherwise. Exercised over real Postgres through the
    # provider's own ``service.users_store()`` seam (resolved to the live DB by accounts_db).
    from tai42_accounts_postgres.provider import PostgresAccountsProvider

    from .conftest import FakeProviderSettings

    provider = PostgresAccountsProvider(FakeProviderSettings())
    assert await provider.has_login("usr-nobody") is False
    uid = await _make_user(UsersStore(accounts_db), "haslogin@a-42.example", "admin", password_hash="argon2$x")
    assert await provider.has_login(uid) is True


async def test_create_read_and_list(accounts_db: PostgresConnectionSettings) -> None:
    store = UsersStore(accounts_db)
    uid = await _make_user(store, "alice@a-42.example", "admin", password_hash="argon2$alice")

    by_email = await store.get_by_email("alice@a-42.example")
    assert by_email is not None
    assert by_email["user_id"] == uid
    assert by_email["role"] == "admin"

    by_id = await store.get_by_user_id(uid)
    assert by_id is not None
    assert by_id["email"] == "alice@a-42.example"

    listed = await store.list()
    assert [u["user_id"] for u in listed] == [uid]
    assert listed[0]["pending_invite"] is False  # a real password hash was written


async def test_duplicate_email_maps_to_email_taken(accounts_db: PostgresConnectionSettings) -> None:
    # The live ``accounts_users_email_unique`` violation must reach the split by name.
    store = UsersStore(accounts_db)
    await _make_user(store, "alice@a-42.example", "admin")
    with pytest.raises(EmailTakenError):
        await _make_user(store, "alice@a-42.example", "member")
    assert len(await store.list()) == 1


async def test_user_id_collision_raises_login_exists(accounts_db: PostgresConnectionSettings) -> None:
    # A real ``accounts_users_user_id_unique`` violation (same id, distinct email) is a
    # loud invariant breach: the principal owns the id, so a second login for it raises
    # rather than landing a divergent row.
    store = UsersStore(accounts_db)
    await store.create_login("usr-fixed", "alice@a-42.example", "admin")
    with pytest.raises(LoginExistsError):
        await store.create_login("usr-fixed", "bob@a-42.example", "member")
    assert len(await store.list()) == 1


async def test_count_other_enabled_admins_excludes_self_and_disabled(
    accounts_db: PostgresConnectionSettings,
) -> None:
    store = UsersStore(accounts_db)
    alice = await _make_user(store, "alice@a-42.example", "admin")
    bob = await _make_user(store, "bob@a-42.example", "admin")
    await _make_user(store, "carol@a-42.example", "member")
    assert await store.count_other_enabled_admins(alice) == 1  # only bob, another enabled admin
    await store.set_disabled(bob, True)
    assert await store.count_other_enabled_admins(alice) == 0  # bob no longer counts


async def test_admin_guard_serializes_last_admin_removal(accounts_db: PostgresConnectionSettings) -> None:
    # Two concurrent guarded demotions of the last two admins: the advisory xact lock
    # forces the re-read/count/mutate to run one at a time against committed state, so
    # exactly one passes and an enabled admin always survives.
    store = UsersStore(accounts_db)
    alice = await _make_user(store, "alice@a-42.example", "admin")
    bob = await _make_user(store, "bob@a-42.example", "admin")

    async def demote(target: str) -> bool:
        async with store.admin_guard_txn() as guard:
            row = await guard.read_target(target)
            assert row is not None
            if row["role"] == "admin" and await guard.count_other_enabled_admins(target) > 0:
                await guard.set_role(target, "member")
                return True
            return False

    results = await asyncio.gather(demote(alice), demote(bob))
    assert sum(results) == 1  # only one demotion is admitted
    assert await store.count_other_enabled_admins("nobody") == 1  # one enabled admin remains


async def test_session_lifecycle_and_absolute_sweep(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    sessions = SessionsStore(accounts_db)
    uid = await _make_user(users, "alice@a-42.example", "admin", password_hash="argon2$alice")

    # An absolute-expired session, then a live mint whose opportunistic sweep reaps it.
    await sessions.create("sess-expired", uid, _past())
    await sessions.create("sess-live", uid, _future())
    assert await sessions.resolve("sess-expired") is None

    resolved = await sessions.resolve("sess-live")
    assert resolved is not None
    assert resolved["user_id"] == uid
    assert resolved["email"] == "alice@a-42.example"  # the JOIN with the user row

    before = resolved["last_seen_at"]
    await sessions.touch("sess-live", _future(120))
    after = await sessions.resolve("sess-live")
    assert after is not None
    assert after["last_seen_at"] > before

    assert await sessions.delete("sess-live") is True
    assert await sessions.delete("sess-live") is False  # already gone


async def test_session_revocation_keeps_presented_token(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    sessions = SessionsStore(accounts_db)
    uid = await _make_user(users, "alice@a-42.example", "admin")
    await sessions.create("keep", uid, _future())
    await sessions.create("drop", uid, _future())

    await sessions.delete_for_user(uid, keep_token_hash="keep")
    assert await sessions.resolve("keep") is not None
    assert await sessions.resolve("drop") is None

    await sessions.delete_for_user(uid)
    assert await sessions.resolve("keep") is None  # the sparing token is revoked too


async def test_invite_consume_is_single_use(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    invites = InvitesStore(accounts_db)
    uid = await _make_user(users, "alice@a-42.example", "member")
    await invites.create("inv-1", uid, _future())
    assert await invites.consume("inv-1", _now()) == uid
    assert await invites.consume("inv-1", _now()) is None  # a replay finds no live row


async def test_invite_expired_is_not_consumable(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    invites = InvitesStore(accounts_db)
    uid = await _make_user(users, "alice@a-42.example", "member")
    await invites.create("inv-ttl", uid, _future(3600))
    # A ``now`` past the invite's expiry: the TTL guard in the UPDATE predicate rejects it.
    assert await invites.consume("inv-ttl", _future(7200)) is None


async def test_invite_create_replaces_prior_for_user(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    invites = InvitesStore(accounts_db)
    uid = await _make_user(users, "alice@a-42.example", "member")
    await invites.create("inv-old", uid, _future())
    await invites.create("inv-new", uid, _future())  # one live invite per user
    assert await invites.consume("inv-old", _now()) is None
    assert await invites.consume("inv-new", _now()) == uid
