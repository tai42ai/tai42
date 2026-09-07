"""Harness-side Postgres: a migrated template database created once per session,
then a fast ``CREATE DATABASE ... TEMPLATE`` clone per stack.

The template schema is built by running the SAME migration chains the product
runs — the skeleton chain plus the accounts-postgres plugin chain — through the
kit migration runner (:func:`tai42_kit.db.apply_migrations`), against the template
database. So the template carries the schema a from-scratch ``tai db migrate``
applies, and the per-stack clones inherit it (the fresh-install leg verifies a
replay against this template by ``public`` table-NAME set, not a byte-for-byte
schema diff). The harness is synchronous
psycopg while the runner is async, so ``ensure_template`` drives it through
``asyncio.run``; the chains carry the same component names and packaged SQL
directories production discovers, differing only in the explicit connection
settings pointing at the target DB rather than the registry's env-prefix
resolution."""

from __future__ import annotations

import asyncio
import importlib.resources

import psycopg
from psycopg import sql
from pydantic import SecretStr
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import MigrationEntry, apply_migrations
from tai42_kit.plugins import parse_plugin_spec
from tai42_skeleton.db.discovery import SKELETON_COMPONENT, skeleton_migrations_dir
from tai42_skeleton.states.db import STATES_COMPONENT, states_migrations_dir

from tai42_e2e.settings import HarnessSettings

_TEMPLATE_DB = "tai42_e2e_template"

# The accounts plugin's import package, holding its packaged ``tai-plugin.yml`` and
# its ``migrations`` chain directory.
_ACCOUNTS_IMPORT_PACKAGE = "tai42_accounts_postgres"


def _accounts_plugin_entry(settings: PostgresConnectionSettings) -> MigrationEntry:
    """The accounts-postgres plugin chain as a runner entry, its SQL directory
    resolved from its packaged ``tai-plugin.yml`` the way production discovery does,
    but pointed at ``settings`` (the explicit target database) rather than the
    registry's env-resolved migrator identity. The plugin declares a ``migrations``
    chain; a missing declaration is a loud error rather than a silently skipped
    schema."""
    package = importlib.resources.files(_ACCOUNTS_IMPORT_PACKAGE)
    spec_path = package.joinpath("tai-plugin.yml")
    spec = parse_plugin_spec(spec_path.read_bytes(), source=str(spec_path))
    if spec.migrations is None:
        raise RuntimeError(
            "tai42-accounts-postgres declares no 'migrations' chain in its tai-plugin.yml; "
            "the e2e template cannot be built from the product chains"
        )
    if spec.package is None:
        raise RuntimeError(
            "tai42-accounts-postgres declares 'migrations' but no 'package' in its tai-plugin.yml; "
            "a migration chain requires a package to import"
        )
    migrations_dir = package.joinpath(*spec.migrations.split("/"))
    return MigrationEntry(component=spec.package, migrations_dir=migrations_dir, settings=settings)


def product_migration_entries(settings: PostgresConnectionSettings) -> list[MigrationEntry]:
    """The migration chains the product applies to a platform database: the
    skeleton chain, the skeleton-owned ``states`` record-store chain, and the
    accounts-postgres plugin chain, all pointed at ``settings``. The order mirrors
    production discovery (``skeleton_entry`` → ``states_entry`` → plugin entries),
    so the harness template matches a from-scratch ``tai db migrate``. Building the
    template DB and the fresh-install replay leg run this exact set."""
    skeleton = MigrationEntry(
        component=SKELETON_COMPONENT,
        migrations_dir=skeleton_migrations_dir(),
        settings=settings,
    )
    states = MigrationEntry(
        component=STATES_COMPONENT,
        migrations_dir=states_migrations_dir(),
        settings=settings,
    )
    return [skeleton, states, _accounts_plugin_entry(settings)]


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

    def _connection_settings(self, dbname: str) -> PostgresConnectionSettings:
        """DDL-privileged connection settings for the migration runner, pointing at
        ``dbname`` on the shared server — explicit per-entry settings, never an
        env-prefix resolution."""
        return PostgresConnectionSettings(
            pg_host=self._settings.pg_host,
            pg_port=self._settings.pg_port,
            pg_db=dbname,
            pg_user=self._settings.pg_user,
            pg_password=SecretStr(self._settings.pg_password),
        )

    def check_reachable(self) -> None:
        """Connect and round-trip ``SELECT 1`` so an unreachable Postgres fails
        loudly at session start with the compose hint, never mid-suite."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            if cur.fetchone() != (1,):
                raise RuntimeError("Postgres SELECT 1 did not return 1")

    def ensure_template(self) -> None:
        """Create the template DB (dropping any stale one) and build its schema
        once per session by running the product migration chains into it."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            self._drop_db(cur, _TEMPLATE_DB)
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_TEMPLATE_DB)))
        asyncio.run(apply_migrations(product_migration_entries(self._connection_settings(_TEMPLATE_DB))))

    def create_stack_db(self, dbname: str) -> None:
        """Clone the template into a per-stack database (migration-free, fast)."""
        with self._admin_conn() as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(dbname), sql.Identifier(_TEMPLATE_DB))
            )

    def create_empty_db(self, dbname: str) -> None:
        """Create a fresh, empty per-service database with NO template — for a
        service that OWNS its own schema and applies its own DDL (unlike the
        migrated-template clones). The marketplace registry's init DDL runs
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
