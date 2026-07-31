"""The thin Postgres seam: one class per table over the shared kit pool.

All SQL lives here; service and route code never write SQL. Unique violations are
split by constraint name so an email conflict and a ``user_id`` collision take
different paths. Expired/consumed rows are swept opportunistically on the write
that would create new ones; the sweep has no private error handling, so a sweep
failure propagates with the enclosing operation.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient

if TYPE_CHECKING:
    from tai42_accounts_postgres.settings import AccountsPgSettings

# Bounded id-collision retry: a small cap that then RAISES, never an unbounded loop.
_MAX_ID_ATTEMPTS = 3

# Fixed advisory-lock key serializing the count-then-mutate operations that must
# not interleave (bootstrap owner insert, last-admin guard). ASCII "ACCOUS".
_ACCOUNTS_ADVISORY_LOCK = 0x414343_4F5553

_EMAIL_UNIQUE = "accounts_users_email_unique"
_USER_ID_UNIQUE = "accounts_users_user_id_unique"


class EmailTakenError(Exception):
    """An account with this (normalized) email already exists."""

    def __init__(self, email: str) -> None:
        super().__init__(f"email already registered: {email!r}")
        self.email = email


def new_user_id() -> str:
    """A fresh opaque, stable user id: ``usr-<token_urlsafe(8)>``."""
    return f"usr-{secrets.token_urlsafe(8)}"


class UsersStore:
    """The ``accounts_users`` table."""

    def __init__(self, settings: AccountsPgSettings) -> None:
        self._settings = settings

    async def create(self, user_id: str, email: str, role: str, password_hash: str | None = None) -> str:
        """Insert a user, returning the id actually written.

        An email-unique violation raises :class:`EmailTakenError`. A user_id-unique
        violation regenerates the id and retries up to ``_MAX_ID_ATTEMPTS`` times,
        then raises.
        """
        attempt_id = user_id
        for _ in range(_MAX_ID_ATTEMPTS):
            try:
                async with (
                    client_ctx(PostgresClient, self._settings) as pool,
                    pool.connection() as conn,
                    conn.cursor() as cur,
                ):
                    await cur.execute(
                        "INSERT INTO accounts_users (user_id, email, password_hash, role) VALUES (%s, %s, %s, %s)",
                        (attempt_id, email, password_hash, role),
                    )
                return attempt_id
            except psycopg.errors.UniqueViolation as exc:
                constraint = exc.diag.constraint_name
                if constraint == _EMAIL_UNIQUE:
                    raise EmailTakenError(email) from exc
                if constraint == _USER_ID_UNIQUE:
                    attempt_id = new_user_id()
                    continue
                raise
        raise RuntimeError(
            f"could not generate a unique user_id after {_MAX_ID_ATTEMPTS} attempts (constraint {_USER_ID_UNIQUE})"
        )

    async def create_owner_if_first(
        self, user_id: str, email: str, password_hash: str, role: str
    ) -> dict[str, Any] | None:
        """Insert the first owner under an advisory lock, or return ``None``.

        One transaction: take the advisory xact lock, count users, and insert only
        when the count is zero. Returns the inserted row, or ``None`` when users
        already exist.
        """
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ACCOUNTS_ADVISORY_LOCK,))
            await cur.execute("SELECT count(*) AS n FROM accounts_users")
            row = await cur.fetchone()
            if row is None or row["n"] != 0:
                return None
            await cur.execute(
                "INSERT INTO accounts_users (user_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING user_id, email, role, disabled, created_at",
                (user_id, email, password_hash, role),
            )
            return await cur.fetchone()

    async def get_by_email(self, email: str) -> dict[str, Any] | None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, password_hash, role, disabled, created_at FROM accounts_users WHERE email = %s",
                (email,),
            )
            return await cur.fetchone()

    async def get_by_user_id(self, user_id: str) -> dict[str, Any] | None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, password_hash, role, disabled, created_at "
                "FROM accounts_users WHERE user_id = %s",
                (user_id,),
            )
            return await cur.fetchone()

    async def list(self) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, role, disabled, created_at, (password_hash IS NULL) AS pending_invite "
                "FROM accounts_users ORDER BY created_at"
            )
            return list(await cur.fetchall())

    async def set_password_hash(self, user_id: str, password_hash: str) -> None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "UPDATE accounts_users SET password_hash = %s WHERE user_id = %s",
                (password_hash, user_id),
            )

    async def set_role(self, user_id: str, role: str) -> None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("UPDATE accounts_users SET role = %s WHERE user_id = %s", (role, user_id))

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("UPDATE accounts_users SET disabled = %s WHERE user_id = %s", (disabled, user_id))

    async def delete(self, user_id: str) -> None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM accounts_users WHERE user_id = %s", (user_id,))

    async def count(self) -> int:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT count(*) AS n FROM accounts_users")
            row = await cur.fetchone()
            return 0 if row is None else int(row["n"])

    async def count_other_enabled_admins(self, user_id: str) -> int:
        """Enabled admins OTHER than ``user_id`` — the last-admin guard's input.

        Zero here for a currently-enabled admin means acting on that user would
        leave the deployment with no enabled admin (caller 409s). Keys on the
        reserved literal ``'admin'``.
        """
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT count(*) AS n FROM accounts_users WHERE role = 'admin' AND disabled = FALSE AND user_id <> %s",
                (user_id,),
            )
            row = await cur.fetchone()
            return 0 if row is None else int(row["n"])

    @asynccontextmanager
    async def admin_guard_txn(self) -> AsyncIterator[_AdminGuard]:
        """One transaction under the fixed advisory lock for a last-admin-guarded
        mutation (disable / demote / delete).

        A concurrent guarded removal blocks at the lock and re-evaluates the admin
        count against committed state, so two removals of the last two admins can
        never both pass. On the yielded guard the caller re-reads, counts, and
        mutates on this one cursor; all commit or roll back together on block exit.
        """
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ACCOUNTS_ADVISORY_LOCK,))
            yield _AdminGuard(cur)


class _AdminGuard:
    """The last-admin re-read, count, and mutation, bound to one advisory-locked
    cursor as a single serialized transaction.

    Every method runs on the guard's own cursor, so the credential cleanup a guarded
    disable/delete performs runs in this transaction — never a second pool checkout
    under the lock.
    """

    def __init__(self, cur: psycopg.AsyncCursor[dict[str, Any]]) -> None:
        self._cur = cur

    async def read_target(self, user_id: str) -> dict[str, Any] | None:
        """The target's committed ``role``/``disabled`` under the lock — the
        authoritative state the orphan check decides on, never the pre-lock
        snapshot."""
        await self._cur.execute(
            "SELECT role, disabled FROM accounts_users WHERE user_id = %s",
            (user_id,),
        )
        return await self._cur.fetchone()

    async def count_other_enabled_admins(self, user_id: str) -> int:
        # Same ``'admin'`` basis as the plain read, on the guard's locked cursor.
        await self._cur.execute(
            "SELECT count(*) AS n FROM accounts_users WHERE role = 'admin' AND disabled = FALSE AND user_id <> %s",
            (user_id,),
        )
        row = await self._cur.fetchone()
        return 0 if row is None else int(row["n"])

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        await self._cur.execute("UPDATE accounts_users SET disabled = %s WHERE user_id = %s", (disabled, user_id))

    async def set_role(self, user_id: str, role: str) -> None:
        await self._cur.execute("UPDATE accounts_users SET role = %s WHERE user_id = %s", (role, user_id))

    async def delete(self, user_id: str) -> None:
        await self._cur.execute("DELETE FROM accounts_users WHERE user_id = %s", (user_id,))

    async def delete_sessions_for_user(self, user_id: str) -> None:
        """Revoke the user's sessions on the guard's connection (same transaction)."""
        await self._cur.execute("DELETE FROM accounts_sessions WHERE user_id = %s", (user_id,))

    async def delete_invites_for_user(self, user_id: str) -> None:
        """Drop the user's invites on the guard's connection (same transaction)."""
        await self._cur.execute("DELETE FROM accounts_invites WHERE user_id = %s", (user_id,))


class SessionsStore:
    """The ``accounts_sessions`` table."""

    def __init__(self, settings: AccountsPgSettings) -> None:
        self._settings = settings

    async def create(self, token_hash: str, user_id: str, absolute_expires_at: Any) -> None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            # Opportunistic sweep of absolute-expired rows; a sweep failure fails the mint.
            await cur.execute("DELETE FROM accounts_sessions WHERE absolute_expires_at <= now()")
            await cur.execute(
                "INSERT INTO accounts_sessions (token_hash, user_id, absolute_expires_at) VALUES (%s, %s, %s)",
                (token_hash, user_id, absolute_expires_at),
            )

    async def resolve(self, token_hash: str) -> dict[str, Any] | None:
        """The session row JOINed with its user, or ``None`` when unknown."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT s.user_id, u.email, u.role, u.disabled, s.last_seen_at, s.absolute_expires_at "
                "FROM accounts_sessions s JOIN accounts_users u ON u.user_id = s.user_id "
                "WHERE s.token_hash = %s",
                (token_hash,),
            )
            return await cur.fetchone()

    async def touch(self, token_hash: str, now: Any) -> None:
        """Write ``last_seen_at`` (the caller applies the 60 s staleness throttle)."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "UPDATE accounts_sessions SET last_seen_at = %s WHERE token_hash = %s",
                (now, token_hash),
            )

    async def delete(self, token_hash: str) -> bool:
        """Delete one session; ``True`` when a row died, ``False`` when unknown."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM accounts_sessions WHERE token_hash = %s", (token_hash,))
            return cur.rowcount > 0

    async def delete_for_user(self, user_id: str, keep_token_hash: str | None = None) -> None:
        """Revoke a user's sessions, optionally sparing one presented token."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            if keep_token_hash is None:
                await cur.execute("DELETE FROM accounts_sessions WHERE user_id = %s", (user_id,))
            else:
                await cur.execute(
                    "DELETE FROM accounts_sessions WHERE user_id = %s AND token_hash <> %s",
                    (user_id, keep_token_hash),
                )


class InvitesStore:
    """The ``accounts_invites`` table."""

    def __init__(self, settings: AccountsPgSettings) -> None:
        self._settings = settings

    async def create(self, token_hash: str, user_id: str, expires_at: Any) -> None:
        """Mint an invite, replacing any prior one for the user (one live invite
        per user) and sweeping consumed/expired rows in the same transaction."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM accounts_invites WHERE user_id = %s", (user_id,))
            await cur.execute("DELETE FROM accounts_invites WHERE consumed_at IS NOT NULL OR expires_at <= now()")
            await cur.execute(
                "INSERT INTO accounts_invites (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (token_hash, user_id, expires_at),
            )

    async def consume(self, token_hash: str, now: Any) -> str | None:
        """Atomically consume a live invite, returning its ``user_id`` or ``None``.

        Single-use and TTL are enforced in the UPDATE predicate (``consumed_at IS
        NULL AND expires_at > now``), so a replay/expired/unknown token returns no row.
        """
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "UPDATE accounts_invites SET consumed_at = %s "
                "WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > %s "
                "RETURNING user_id",
                (now, token_hash, now),
            )
            row = await cur.fetchone()
            return None if row is None else row["user_id"]

    async def delete_for_user(self, user_id: str) -> None:
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM accounts_invites WHERE user_id = %s", (user_id,))
