"""``tai doctor`` — read-only environment health diagnostics."""

from __future__ import annotations

import asyncio
import platform
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit, urlunsplit

import typer
from tai42_cli.commands._common import app_context
from tai42_cli.render import print_json, render_table
from tai42_kit.clients import PostgresConnectionSettings, client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.db import (
    AdminIdentityIncompleteError,
    DatabaseNotConfiguredError,
    MigrationError,
    component_migrator_settings,
    migration_status,
)

from tai42_skeleton.config.config_mode import config_mode
from tai42_skeleton.db import SKELETON_COMPONENT, all_migration_entries

_OK = "ok"
_FAIL = "fail"
_INFO = "info"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _pg_target(settings: PostgresConnectionSettings) -> str:
    """A credential-free description of the Postgres target."""
    return f"{settings.pg_host}:{settings.pg_port}/{settings.pg_db}"


def _redact_url(url: str | None) -> str:
    """Mask the password in a connection URL, keeping the rest for diagnosis."""
    if not url:
        return "(unset)"
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    netloc = f"{user}:***@{host}" if user else f":***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _probe_postgres(settings: PostgresConnectionSettings) -> Check:
    import psycopg

    try:
        async with (
            client_ctx(PostgresClient, settings, fresh=True) as pool,
            pool.connection() as conn,
        ):
            await conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        return Check("postgres", _FAIL, f"cannot connect to {_pg_target(settings)}: {exc}")
    except ValueError as exc:
        # The kit's named malformed-connection error — a clean check line, never a
        # traceback.
        return Check("postgres", _FAIL, str(exc))
    return Check("postgres", _OK, f"connected to {_pg_target(settings)}")


async def _probe_schema(settings: PostgresConnectionSettings) -> Check:
    """Report per-component migration-chain status via the runner.

    ``migration_status`` never raises on a mismatch — a pending migration or a
    rewritten (checksum-diverged) chain comes back as data, rendered here as a FAIL
    naming ``tai db migrate``. A connection failure, a malformed installed-plugin
    chain (a kit :class:`~tai42_kit.db.MigrationError` from discovery/checksum), or a
    component bound to an unconfigured named database (a kit
    :class:`~tai42_kit.db.DatabaseNotConfiguredError`, which names the env var) or one
    with a half-set admin identity (a kit
    :class:`~tai42_kit.db.AdminIdentityIncompleteError`, which names both admin vars)
    is caught and rendered as a FAIL so the diagnostic stays a diagnostic; every other
    error propagates so a broken probe is never mistaken for a clean schema."""
    import psycopg

    try:
        entries = await all_migration_entries()
        statuses = await migration_status(entries)
    except psycopg.OperationalError as exc:
        return Check("schema", _FAIL, f"cannot inspect schema at {_pg_target(settings)}: {exc}")
    except MigrationError as exc:
        return Check("schema", _FAIL, f"cannot inspect schema (run 'tai db migrate'): {exc}")
    except (DatabaseNotConfiguredError, AdminIdentityIncompleteError) as exc:
        return Check("schema", _FAIL, str(exc))
    stale = [status for status in statuses if not status.is_up_to_date]
    if stale:
        detail = ", ".join(
            f"{status.component} ({len(status.pending)} pending, {len(status.mismatches)} mismatch)" for status in stale
        )
        return Check("schema", _FAIL, f"out of date (run 'tai db migrate'): {detail}")
    return Check("schema", _OK, f"all {len(statuses)} component(s) up to date")


async def _probe_redis() -> Check:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from tai42_skeleton.connectors.settings import connector_store_settings

    redis_settings = connector_store_settings().redis
    target = _redact_url(redis_settings.redis_url)
    try:
        async with client_ctx(RedisClient, redis_settings, fresh=True) as client:
            # redis-py types ``ping`` against its sync client (-> bool); the async
            # client returns the awaitable it is called on here.
            await cast("Awaitable[bool]", client.ping())
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
        return Check("redis", _FAIL, f"cannot connect to {target}: {exc}")
    except ValueError as exc:
        # The kit's named not-configured error (an unset CONNECTOR_STORE_REDIS_URL
        # with no TAI_DEFAULT_REDIS_URL) — a clean check line, never a traceback.
        return Check("redis", _FAIL, str(exc))
    return Check("redis", _OK, f"connected to {target}")


async def _run_checks() -> list[Check]:
    checks = [
        Check("python", _INFO, platform.python_version()),
        Check("config-mode", _INFO, config_mode()),
    ]
    try:
        settings = component_migrator_settings(SKELETON_COMPONENT)
    except (DatabaseNotConfiguredError, AdminIdentityIncompleteError) as exc:
        # The skeleton database is unconfigured or its admin identity is half-set; the
        # schema probe has nothing to inspect, so report the postgres check as a clean
        # FAIL and skip it.
        checks.append(Check("postgres", _FAIL, str(exc)))
        checks.append(await _probe_redis())
        return checks
    postgres = await _probe_postgres(settings)
    checks.append(postgres)
    # The schema probe needs a live connection; skip it (rather than double-report
    # the connection failure) when Postgres itself is unreachable.
    if postgres.status == _OK:
        checks.append(await _probe_schema(settings))
    checks.append(await _probe_redis())
    return checks


def doctor(ctx: typer.Context) -> None:
    """Run read-only health diagnostics against the environment.

    Probes Python version, config mode, Postgres and Redis connectivity, and
    whether the schema is applied. Purely read-only. Credentials are redacted:
    connection-URL passwords are masked and no DSN is echoed. Exits non-zero when
    any dependency check fails.
    """
    app_ctx = app_context(ctx)
    checks = asyncio.run(_run_checks())
    records = [{"check": c.name, "status": c.status, "detail": c.detail} for c in checks]

    if app_ctx.json_output:
        print_json(records)
    else:
        typer.echo(render_table(records, ["check", "status", "detail"]))

    if any(c.status == _FAIL for c in checks):
        raise typer.Exit(1)
