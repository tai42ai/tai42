"""The thin Postgres seam: one class per table over the shared kit pool.

All SQL lives here; service and route code never write SQL. Unique violations are
split by constraint name so an email conflict and a ``user_id`` collision take
different paths. Expired/consumed rows are swept opportunistically on the write
that would create new ones; the sweep has no private error handling, so a sweep
failure propagates with the enclosing operation.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from tai42_contract.accounts.errors import LoginConflictError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient, read_connection

if TYPE_CHECKING:
    from tai42_kit.clients import PostgresConnectionSettings

_EMAIL_UNIQUE = "accounts_users_email_unique"
_USER_ID_UNIQUE = "accounts_users_user_id_unique"


class EmailTakenError(LoginConflictError):
    """An account with this (normalized) email already exists.

    A :class:`~tai42_contract.accounts.errors.LoginConflictError` so a platform
    caller (the setup door) maps it to a conflict without importing this
    provider-private type.
    """

    def __init__(self, email: str) -> None:
        """Record the ``email`` that was already registered."""
        super().__init__(f"email already registered: {email!r}")
        self.email = email


class LoginExistsError(LoginConflictError):
    """A login (``accounts_users`` row) already exists for this principal ``user_id``.

    A :class:`~tai42_contract.accounts.errors.LoginConflictError` so a platform
    caller (the setup door) maps it to a conflict without importing this
    provider-private type.
    """

    def __init__(self, user_id: str) -> None:
        """Record the ``user_id`` whose login already exists."""
        super().__init__(f"a login already exists for principal {user_id!r}")
        self.user_id = user_id


def new_user_id() -> str:
    """A fresh opaque, stable user id: ``usr-<token_urlsafe(8)>``."""
    return f"usr-{secrets.token_urlsafe(8)}"


class UsersStore:
    """The ``accounts_users`` table."""

    def __init__(self, settings: PostgresConnectionSettings) -> None:
        """Bind the store to the Postgres connection ``settings``."""
        self._settings = settings

    async def create_login(self, user_id: str, email: str, password_hash: str | None = None) -> None:
        """Insert the login row for the EXISTING principal ``user_id``.

        A ``user_id``-unique violation raises :class:`LoginExistsError` (a login
        already exists for that principal); an email-unique violation raises
        :class:`EmailTakenError`. The principal owns ``user_id``, so a collision is
        an invariant breach surfaced loudly, never silently regenerated.
        """
        try:
            async with (
                client_ctx(PostgresClient, self._settings) as pool,
                pool.connection() as conn,
                conn.cursor() as cur,
            ):
                await cur.execute(
                    "INSERT INTO accounts_users (user_id, email, password_hash) VALUES (%s, %s, %s)",
                    (user_id, email, password_hash),
                )
        except psycopg.errors.UniqueViolation as exc:
            constraint = exc.diag.constraint_name
            if constraint == _EMAIL_UNIQUE:
                raise EmailTakenError(email) from exc
            if constraint == _USER_ID_UNIQUE:
                raise LoginExistsError(user_id) from exc
            raise

    async def get_by_email(self, email: str) -> dict[str, Any] | None:
        """The user row for ``email``, or ``None`` when none exists."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, password_hash, disabled, created_at FROM accounts_users WHERE email = %s",
                (email,),
            )
            return await cur.fetchone()

    async def get_by_user_id(self, user_id: str) -> dict[str, Any] | None:
        """The user row for ``user_id``, or ``None`` when none exists."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, password_hash, disabled, created_at FROM accounts_users WHERE user_id = %s",
                (user_id,),
            )
            return await cur.fetchone()

    async def list(self) -> list[dict[str, Any]]:
        """Every user row, oldest first, each with a ``pending_invite`` flag for a password-less account."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT user_id, email, disabled, created_at, (password_hash IS NULL) AS pending_invite "
                "FROM accounts_users ORDER BY created_at"
            )
            return list(await cur.fetchall())

    async def set_password_hash(self, user_id: str, password_hash: str) -> None:
        """Set ``user_id``'s password hash."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "UPDATE accounts_users SET password_hash = %s WHERE user_id = %s",
                (password_hash, user_id),
            )

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        """Enable or disable ``user_id``."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("UPDATE accounts_users SET disabled = %s WHERE user_id = %s", (disabled, user_id))

    async def delete(self, user_id: str) -> None:
        """Delete the user ``user_id``."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM accounts_users WHERE user_id = %s", (user_id,))


class SessionsStore:
    """The ``accounts_sessions`` table."""

    def __init__(self, settings: PostgresConnectionSettings) -> None:
        """Bind the store to the Postgres connection ``settings``."""
        self._settings = settings

    async def create(self, token_hash: str, user_id: str, absolute_expires_at: Any) -> None:
        """Mint a session for ``user_id``, sweeping absolute-expired rows in the same transaction."""
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
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT s.user_id, u.email, u.disabled, s.last_seen_at, s.absolute_expires_at "
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

    def __init__(self, settings: PostgresConnectionSettings) -> None:
        """Bind the store to the Postgres connection ``settings``."""
        self._settings = settings

    async def create(self, token_hash: str, user_id: str, expires_at: Any) -> None:
        """Mint an invite, replacing any prior one for the user (one live invite per user).

        Sweeps consumed/expired rows in the same transaction.
        """
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
        """Drop every invite for ``user_id``."""
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM accounts_invites WHERE user_id = %s", (user_id,))

    async def list_open(self) -> list[dict[str, Any]]:
        """Every open (unconsumed) invitation with its invited person, oldest first.

        Joins the invitation to its ``accounts_users`` row so each carries the invited
        ``email`` and the user's ``created_at``, plus the invitation's own
        ``expires_at``. An invitation past ``expires_at`` is still listed (so an admin
        can see it and regenerate); a consumed one is excluded — its person has a
        password and is a member, not a pending invite.
        """
        async with (
            client_ctx(PostgresClient, self._settings) as pool,
            read_connection(pool) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT u.user_id, u.email, u.created_at, i.expires_at "
                "FROM accounts_invites i JOIN accounts_users u ON u.user_id = i.user_id "
                "WHERE i.consumed_at IS NULL ORDER BY u.created_at"
            )
            return list(await cur.fetchall())
