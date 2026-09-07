"""A REAL Postgres exercise of the ``accounts`` backup section: the settings-tier
roster round-trips through export/import with password hashes and timestamps intact,
restore is skip-only, and an email that collides with a DIFFERENT existing user is a
per-user error the savepoint contains while its siblings still restore.

The per-user email-collision path depends on a live ``accounts_users_email_unique``
violation, which the scripted cursor can only imitate. OPT-IN: set
``TAI42_ACCOUNTS_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live
Postgres; without it the tests SKIP VISIBLY (never a silent skip)."""

from __future__ import annotations

import pytest
from tai42_kit.clients import PostgresConnectionSettings

from tai42_accounts_postgres.backup import export_accounts, import_accounts
from tai42_accounts_postgres.stores import UsersStore

pytestmark = pytest.mark.integration


async def test_export_import_roundtrip_and_skip_only(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    await users.create("usr-alice", "alice@a-42.example", "admin", password_hash="argon2$alice")
    await users.create("usr-bob", "bob@a-42.example", "member")  # a pending invite: null hash
    original = await users.get_by_user_id("usr-alice")
    assert original is not None

    payload = await export_accounts()
    assert payload["version"] == 1
    assert {u["user_id"] for u in payload["users"]} == {"usr-alice", "usr-bob"}

    # Wipe the roster (a fresh restore target) and import it back.
    await users.delete("usr-alice")
    await users.delete("usr-bob")
    assert await users.count() == 0

    report = await import_accounts(payload)
    assert report["created"] == 2
    assert report["skipped_existing"] == 0
    assert report["errors"] == []

    restored = await users.get_by_user_id("usr-alice")
    assert restored is not None
    assert restored["password_hash"] == "argon2$alice"  # the verifier round-trips as stored
    assert restored["role"] == "admin"
    assert restored["created_at"] == original["created_at"]  # timestamp preserved through iso round-trip

    pending = await users.get_by_user_id("usr-bob")
    assert pending is not None
    assert pending["password_hash"] is None

    # Skip-only: a second import of the same payload creates nothing.
    again = await import_accounts(payload)
    assert again["created"] == 0
    assert again["skipped_existing"] == 2
    assert again["errors"] == []


async def test_import_email_collision_is_contained_per_user(accounts_db: PostgresConnectionSettings) -> None:
    users = UsersStore(accounts_db)
    # An existing user already holds the email a payload row (with a different user_id) claims.
    await users.create("usr-existing", "shared@a-42.example", "admin", password_hash="argon2$existing")
    payload = {
        "version": 1,
        "users": [
            {
                "user_id": "usr-clash",
                "email": "shared@a-42.example",
                "password_hash": "argon2$clash",
                "role": "member",
                "disabled": False,
                "created_at": "2026-01-02T03:04:05+00:00",
            },
            {
                "user_id": "usr-ok",
                "email": "ok@a-42.example",
                "password_hash": "argon2$ok",
                "role": "member",
                "disabled": False,
                "created_at": "2026-01-02T03:04:05+00:00",
            },
        ],
    }

    report = await import_accounts(payload)
    assert report["created"] == 1  # only usr-ok landed
    assert report["skipped_existing"] == 0
    assert len(report["errors"]) == 1
    assert "usr-clash" in report["errors"][0]

    assert await users.get_by_user_id("usr-clash") is None  # the savepoint rolled it back
    assert await users.get_by_user_id("usr-ok") is not None  # its sibling still restored
