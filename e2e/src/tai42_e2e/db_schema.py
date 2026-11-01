"""What a database's schema holds, read over a direct connection: its tables and its migration history."""

from __future__ import annotations

import psycopg

from tai42_e2e.settings import HarnessSettings


def connect(harness: HarnessSettings, dbname: str) -> psycopg.Connection:
    """A connection to ``dbname`` on the harness Postgres, as the harness's own user."""
    return psycopg.connect(
        host=harness.pg_host,
        port=harness.pg_port,
        user=harness.pg_user,
        password=harness.pg_password,
        dbname=dbname,
    )


def public_tables(harness: HarnessSettings, dbname: str) -> set[str]:
    """The names of the base tables in ``dbname``'s ``public`` schema."""
    with connect(harness, dbname) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {row[0] for row in cur.fetchall()}


def history_components(harness: HarnessSettings, dbname: str) -> set[str]:
    """Every component ``dbname``'s ``tai_schema_history`` records a migration of."""
    with connect(harness, dbname) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT component FROM tai_schema_history")
        return {row[0] for row in cur.fetchall()}
