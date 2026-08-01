"""C-accounts — the plugin's own DDL apply command, end-to-end and idempotent.

The accounts plugin ships its schema apply as a module command
(``python -m tai42_accounts_postgres.db apply``) rather than an external migration
tool. Run against a live stack database it is idempotent — a second apply is a
clean no-op that leaves the table set unchanged — and against an unreachable DSN
it exits non-zero, surfacing the failure rather than silently succeeding."""

from __future__ import annotations

import subprocess
import sys

import psycopg

from tai42_e2e.stack import TaiStack

_APPLY = [sys.executable, "-m", "tai42_accounts_postgres.db", "apply"]


def _pg_env(stack: TaiStack, *, host: str | None = None, port: str | None = None) -> dict[str, str]:
    """The plugin's ``TAI_ACCOUNTS_PG_*`` connection env pointed at this stack's
    live database, optionally re-pointed at an unreachable endpoint."""
    resources = stack.resources
    return {
        "TAI_ACCOUNTS_PG_HOST": host if host is not None else resources.pg_host,
        "TAI_ACCOUNTS_PG_PORT": port if port is not None else str(resources.pg_port),
        "TAI_ACCOUNTS_PG_DB": resources.pg_db,
        "TAI_ACCOUNTS_PG_USER": resources.pg_user,
        "TAI_ACCOUNTS_PG_PASSWORD": resources.pg_password,
    }


def _accounts_tables(stack: TaiStack) -> set[str]:
    with (
        psycopg.connect(
            host=stack.resources.pg_host,
            port=stack.resources.pg_port,
            user=stack.resources.pg_user,
            password=stack.resources.pg_password,
            dbname=stack.resources.pg_db,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'accounts_%'"
        )
        return {row[0] for row in cur.fetchall()}


def _run(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_overrides}
    # An unreachable DSN is bounded by the connection pool's acquire timeout (~30s),
    # so allow generous headroom over that before the harness would call it a hang.
    return subprocess.run(_APPLY, env=env, capture_output=True, text=True, timeout=120)


def test_apply_is_idempotent_and_fails_loud_on_unreachable_dsn(accounts_stack: TaiStack) -> None:
    stack = accounts_stack

    # The plugin schema is already present (the template clone carries it), so the
    # apply command exercises its full path against a live DB and is a clean success.
    first = _run(_pg_env(stack))
    assert first.returncode == 0, f"apply failed: rc={first.returncode} out={first.stdout!r} err={first.stderr!r}"
    before = _accounts_tables(stack)
    assert {"accounts_users", "accounts_sessions", "accounts_invites"} <= before, before

    # A second apply is a clean no-op (exit 0) and leaves the table set unchanged.
    second = _run(_pg_env(stack))
    assert second.returncode == 0, f"second apply failed: rc={second.returncode} err={second.stderr!r}"
    assert _accounts_tables(stack) == before, "the second apply changed the accounts table set"

    # Pointed at an unreachable DSN, the command exits non-zero and surfaces the
    # error — never a silent success.
    unreachable = _run(_pg_env(stack, host="127.0.0.1", port="1"))
    assert unreachable.returncode != 0, f"apply against an unreachable DSN must fail: {unreachable.returncode}"
    assert (unreachable.stderr or unreachable.stdout).strip(), "a failed apply must surface an error message"
