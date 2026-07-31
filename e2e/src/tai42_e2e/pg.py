"""Harness-side Postgres: a DDL-applied template database created once per
session, then a fast ``CREATE DATABASE ... TEMPLATE`` clone per stack.

The canonical schema is the skeleton's own idempotent DDL — imported from
``tai42_skeleton.sql.schema.load_ddl`` so the harness never carries a second copy
that could drift. The accounts plugin owns its own tables and ships them the same
way, so its ``tai42_accounts_postgres.db.load_ddl`` is applied right after the
skeleton's, into the same template. (These are the only plugin/skeleton imports
the harness makes: it reads the shipped DDL text, it does not run SUT request
logic.) Neither applies DDL at startup, so the harness owns applying it, and the
per-stack clones inherit both schemas."""

from __future__ import annotations

from typing import LiteralString, cast

import psycopg
from psycopg import sql

from tai42_e2e.settings import HarnessSettings

_TEMPLATE_DB = "tai42_e2e_template"


class PostgresAdmin:
    """Creates/drops the template and per-stack databases on the shared server."""

    def __init__(self, settings: HarnessSettings) -> None:
        self._settings = settings

    def _admin_conn(self) -> psycopg.Connection:
        # CREATE/DROP DATABASE cannot run inside a transaction block, so the
        # admin connection is autocommit.
        conn = psycopg.connect(
            host=self._settings.pg_host,
            port=self._settings.pg_port,
            user=self._settings.pg_user,
            password=self._settings.pg_password,
            dbname=self._settings.pg_admin_db,
            autocommit=True,
        )
        return conn

    def check_reachable(self) -> None:
        """Connect and round-trip ``SELECT 1`` so an unreachable Postgres fails
        loudly at session start with the compose hint, never mid-suite."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            if cur.fetchone() != (1,):
                raise RuntimeError("Postgres SELECT 1 did not return 1")

    def ensure_template(self) -> None:
        """Create the template DB (dropping any stale one) and apply the skeleton
        and accounts-plugin DDL into it exactly once per session."""
        from tai42_accounts_postgres.db import load_ddl as load_accounts_ddl
        from tai42_skeleton.sql.schema import load_ddl as load_skeleton_ddl

        with self._admin_conn() as conn, conn.cursor() as cur:
            self._drop_db(cur, _TEMPLATE_DB)
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_TEMPLATE_DB)))
        skeleton_ddl = load_skeleton_ddl()
        accounts_ddl = load_accounts_ddl()
        with psycopg.connect(
            host=self._settings.pg_host,
            port=self._settings.pg_port,
            user=self._settings.pg_user,
            password=self._settings.pg_password,
            dbname=_TEMPLATE_DB,
        ) as conn:
            with conn.cursor() as cur:
                # Both are trusted, shipped schema text (the skeleton's own tables
                # and the accounts plugin's own ``accounts_*`` tables); the plugin
                # DDL is idempotent, so applying it unconditionally is safe.
                cur.execute(cast(LiteralString, skeleton_ddl))
                cur.execute(cast(LiteralString, accounts_ddl))
            conn.commit()

    def create_stack_db(self, dbname: str) -> None:
        """Clone the template into a per-stack database (DDL-free, fast)."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(dbname), sql.Identifier(_TEMPLATE_DB))
            )

    def create_empty_db(self, dbname: str) -> None:
        """Create a fresh, empty per-service database with NO template — for a
        service that OWNS its own schema and applies its own DDL (unlike the
        skeleton-DDL template clones). The marketplace registry's init DDL runs
        ``CREATE EXTENSION pg_trgm``, which needs an owned database rather than a
        clone of the shared template."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))

    def drop_stack_db(self, dbname: str) -> None:
        """Drop a per-stack database at teardown, terminating any leftover
        backends first. A failure here is logged loudly by the caller, never
        swallowed."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            self._drop_db(cur, dbname)

    @staticmethod
    def _drop_db(cur: psycopg.Cursor, dbname: str) -> None:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
