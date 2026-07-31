"""Boundary between what the unit suite proves and what the e2e leg owns.

Proven here against the fakes: the provider, login, bootstrap, invite, and
users-route matrices, the single registration landing in both registries, and the
``db apply`` CLI. NOT unit-provable, owned by the e2e leg against a live Postgres:
real SQL correctness (the UNIQUE constraints, ``pg_advisory_xact_lock`` under true
concurrency, the atomic invite UPDATE, the sessions⋈users JOIN) and the full
request→middleware→provider integration.
"""

from __future__ import annotations

from tai42_accounts_postgres import stores
from tai42_accounts_postgres.db import load_ddl


def test_store_sql_names_match_the_ddl():
    """Drift tripwire: every table/column the stores read or write must appear in
    the packaged DDL."""
    ddl = load_ddl()
    for table in ("accounts_users", "accounts_sessions", "accounts_invites"):
        assert table in ddl
    for column in (
        "user_id",
        "email",
        "password_hash",
        "role",
        "disabled",
        "token_hash",
        "absolute_expires_at",
        "last_seen_at",
        "expires_at",
        "consumed_at",
    ):
        assert column in ddl
    # The constraint names the store splits on must be the ones declared.
    assert stores._EMAIL_UNIQUE in ddl
    assert stores._USER_ID_UNIQUE in ddl
