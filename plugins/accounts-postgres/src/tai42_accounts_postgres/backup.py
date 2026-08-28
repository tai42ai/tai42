"""The ``accounts`` backup section — the plugin's settings-tier account roster.

Registers a section on the host's ``AppBackup`` registry (reached through the
contract facet ``tai42_app.backup``, never by importing the skeleton) so
``tai backup export/import`` carries user accounts across a deployment.

SETTINGS TIER — only ``accounts_users`` (the durable "who has an account" roster)
is exported. ``accounts_sessions`` (live logins) and ``accounts_invites`` (short-
lived pending tokens) are RUNTIME/transient and deliberately excluded, exactly as
checkpoints and conversation records are excluded from the host's own sections.

RESTORE IS SKIP-ONLY — an existing ``user_id`` is left untouched; only users absent
on the target are created. This mirrors the access-control section, whose credential
tokens are likewise "skip-only under every mode" (a credential is never silently
overwritten by a restore). It also sidesteps a real contract limitation: the import
mode (``skip``/``overwrite``) lives in the skeleton's backup registry and is not
exposed on the contract ``AppBackup`` facet, so a contract-facing plugin cannot read
it — skip (the non-destructive default) is the only mode a plugin can honor correctly.
To REPLACE an account from a backup, delete it first, then import.

``password_hash`` is a one-way VERIFIER, not a recoverable secret, so it round-trips
as stored column data — a restored user logs in with the SAME password (unlike an
API key, which cannot verify a re-presented raw key and is re-minted). The section is
``secret=True``: the payload carries password hashes and emails.

Registration runs in an ``on_startup`` hook (the facet is wired only on a booted app)
and is idempotent, so a plugin reload cannot trip the registry's duplicate-name guard
(the host keeps ONE backup registry across reloads).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from tai42_contract.app import tai42_app
from tai42_kit.clients import PostgresConnectionSettings, client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_accounts_postgres.db import COMPONENT, accounts_store_configured

# The section name in the backup manifest + the payload schema version. A payload
# from a NEWER schema is refused loudly rather than mis-restored under old rules.
_SECTION = "accounts"
_VERSION = 1


class _BackupUserStore:
    """The ``accounts_users`` read/restore seam for the backup section — its own
    class so it takes explicit settings and is unit-testable against a scripted
    cursor, exactly like the stores in :mod:`tai42_accounts_postgres.stores`."""

    def __init__(self, settings: PostgresConnectionSettings) -> None:
        self._settings = settings

    async def export_users(self) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, password_hash, role, disabled, created_at "
                "FROM accounts_users ORDER BY created_at, user_id"
            )
            rows = await cur.fetchall()
        return [
            {
                "user_id": r["user_id"],
                "email": r["email"],
                "password_hash": r["password_hash"],
                "role": r["role"],
                "disabled": r["disabled"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    async def restore_users(self, users: list[dict[str, Any]]) -> dict[str, Any]:
        """Create each absent user under its own savepoint (skip-only): an existing
        ``user_id`` is a clean ``skipped_existing``; an email that collides with a
        DIFFERENT existing user is a per-user error (the savepoint rolls it back), so
        one bad row never poisons the rest."""
        report: dict[str, Any] = {"created": 0, "skipped_existing": 0, "errors": []}
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
        ):
            for user in users:
                try:
                    created = await self._create_if_absent(conn, user)
                except psycopg.errors.UniqueViolation as exc:
                    # An email already held by a different user_id — loud per-user
                    # rejection, the rest still restore.
                    report["errors"].append(f"user {user.get('user_id')!r}: {exc.diag.constraint_name or exc}")
                    continue
                except (KeyError, TypeError) as exc:
                    report["errors"].append(f"malformed user record {user!r}: {exc}")
                    continue
                report["created" if created else "skipped_existing"] += 1
        return report

    async def _create_if_absent(self, conn: Any, user: dict[str, Any]) -> bool:
        params = (
            user["user_id"],
            user["email"],
            user.get("password_hash"),
            user["role"],
            bool(user.get("disabled", False)),
            _parse_ts(user.get("created_at")),
        )
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO accounts_users (user_id, email, password_hash, role, disabled, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
                params,
            )
            row = await cur.fetchone()
        return row is not None


def _parse_ts(value: Any) -> Any:
    """A payload timestamp (ISO string) back to a ``datetime`` psycopg adapts to
    ``timestamptz``; a missing value defers to the column default."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


async def export_accounts() -> dict[str, Any]:
    """The section exporter: the account roster, or an empty roster when the accounts
    store is not configured on this deployment (no live schema to read)."""
    if not accounts_store_configured():
        return {"version": _VERSION, "users": []}
    users = await _BackupUserStore(component_store_settings(COMPONENT)).export_users()
    return {"version": _VERSION, "users": users}


async def import_accounts(payload: dict[str, Any]) -> dict[str, Any]:
    """The section importer: restore absent users from ``payload`` (skip-only),
    returning per-entity counts. A payload from a newer schema version is refused."""
    version = payload.get("version")
    if not isinstance(version, int) or version > _VERSION:
        raise ValueError(f"accounts backup payload version {version!r} is newer than this plugin supports ({_VERSION})")
    if not accounts_store_configured():
        raise RuntimeError(
            "cannot import the accounts section: the accounts store is not configured on this deployment"
        )
    users = payload.get("users") or []
    return await _BackupUserStore(component_store_settings(COMPONENT)).restore_users(users)


@tai42_app.lifecycle.on_startup
def register_accounts_backup_section() -> None:
    """Register the ``accounts`` section once per boot. Idempotent: the host keeps one
    backup registry across reloads, so a re-run skips rather than tripping the
    duplicate-name guard."""
    if any(section.name == _SECTION for section in tai42_app.backup.sections()):
        return
    tai42_app.backup.register_section(_SECTION, export_accounts, import_accounts, secret=True)
