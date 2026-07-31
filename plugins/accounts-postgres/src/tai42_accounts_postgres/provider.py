"""``PostgresAccountsProvider`` — session-token validation, login methods, bootstrap.

Storage is the plugin's own Postgres schema; login throttling and the shared
bootstrap token live in the injected Redis. Registered at import.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tai42_contract.access_control.identity import AuthIdentity, ReadinessTarget
from tai42_contract.accounts import (
    AccountsProvider,
    FormField,
    FormMethod,
    LoginMethod,
    register_accounts_provider,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient

from tai42_accounts_postgres import service
from tai42_accounts_postgres.db import declared_tables
from tai42_accounts_postgres.settings import accounts_settings

if TYPE_CHECKING:
    from tai42_contract.accounts import AccountsProviderSettings

logger = logging.getLogger(__name__)

# ``last_seen_at`` is written only when more than this stale, sparing a PG UPDATE
# per request on a busy session.
_TOUCH_THROTTLE_SECONDS = 60

_OPEN_WINDOW_WARNING = (
    "first-owner bootstrap is OPEN — anyone who can reach /api/login/bootstrap can seize the admin "
    "owner; unset TAI_ACCOUNTS_BOOTSTRAP_OPEN to gate it"
)
_SCHEMA_MISSING = (
    "tai42-accounts-postgres schema missing: run 'python -m tai42_accounts_postgres.db apply' "
    "against the configured database"
)


class PostgresAccountsProvider(AccountsProvider):
    """Validate sessions, declare login methods, and own the bootstrap gate."""

    def __init__(self, settings: AccountsProviderSettings) -> None:
        self.settings = settings
        # Populate the module holder so route handlers can reach ``settings``.
        service.set_provider_settings(settings)

    async def validate_token(self, token: str) -> AuthIdentity | None:
        # Fast-reject non-session tokens without a DB hit so resolution moves on.
        if not token.startswith(service.SESSION_TOKEN_PREFIX):
            return None

        token_hash = service.token_hash(token)
        store = service.sessions_store()
        try:
            row = await store.resolve(token_hash)
        except Exception as exc:
            # Fail closed: RAISE, never return None (which reads as invalid-credential).
            logger.error("accounts: session resolve failed: %s", exc)
            raise

        if row is None:
            return None

        now = datetime.now(UTC)
        user_id = row["user_id"]

        if row["disabled"]:
            # Leave the row so an admin can re-enable; the disabled join kills the
            # live session on its next request regardless.
            logger.info("accounts: refused session for disabled user %s", user_id)
            return None
        if now >= row["absolute_expires_at"]:
            logger.info("accounts: session for %s past absolute expiry; deleting", user_id)
            await store.delete(token_hash)
            return None
        idle = (now - row["last_seen_at"]).total_seconds()
        if idle >= accounts_settings().session_idle_seconds:
            logger.info("accounts: session for %s idle-expired; deleting", user_id)
            await store.delete(token_hash)
            return None

        if idle > _TOUCH_THROTTLE_SECONDS:
            await store.touch(token_hash, now)

        return AuthIdentity(
            user_id=user_id,
            claims={"email": row["email"], "role": row["role"], "kind": "session"},
        )

    def login_methods(self) -> list[LoginMethod]:
        # Static config-derived metadata. The bootstrap form declares a
        # bootstrap_token field unless the gate is explicitly opened.
        settings = accounts_settings()

        bootstrap_fields = [
            FormField(name="email", label="Email", autocomplete="email"),
            FormField(name="password", label="Password", secret=True, autocomplete="new-password"),
        ]
        if not settings.bootstrap_open:
            bootstrap_fields.append(FormField(name="bootstrap_token", label="Bootstrap token", secret=True))

        return [
            FormMethod(
                id="password",
                title="Sign in",
                purpose="login",
                fields=[
                    FormField(name="email", label="Email", autocomplete="email"),
                    FormField(name="password", label="Password", secret=True, autocomplete="current-password"),
                ],
                submit_path="/api/login/password",
            ),
            FormMethod(
                id="bootstrap",
                title="Create the first owner",
                purpose="bootstrap",
                fields=bootstrap_fields,
                submit_path="/api/login/bootstrap",
            ),
            FormMethod(
                id="invite",
                title="Set your password",
                purpose="invite",
                fields=[
                    FormField(name="password", label="Password", secret=True, autocomplete="new-password"),
                    FormField(
                        name="password_confirm",
                        label="Confirm password",
                        secret=True,
                        autocomplete="new-password",
                    ),
                ],
                submit_path="/api/login/invite/accept",
            ),
        ]

    async def needs_bootstrap(self) -> bool:
        # Live count so the owner screen disappears the moment the owner exists.
        return await service.users_store().count() == 0

    async def revoke_session(self, token: str) -> bool:
        if not token.startswith(service.SESSION_TOKEN_PREFIX):
            # Not ours — the logout dispatcher moves on.
            return False
        return await service.sessions_store().delete(service.token_hash(token))

    async def healthcheck(self) -> None:
        # Runs once at boot: schema guard, a trivial session query, and the
        # once-per-deployment bootstrap-token fix.
        await self._assert_schema()

        settings = accounts_settings()
        if settings.bootstrap_open:
            # The only ungated config: warn loudly every boot while no owner exists.
            if await self.needs_bootstrap():
                logger.warning(_OPEN_WINDOW_WARNING)
        else:
            await service.ensure_bootstrap_token(self.settings.redis)

    async def _assert_schema(self) -> None:
        expected = declared_tables()
        async with (
            client_ctx(PostgresClient, accounts_settings().pg) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (expected,),
            )
            present = {row[0] for row in await cur.fetchall()}
            if any(table not in present for table in expected):
                raise RuntimeError(_SCHEMA_MISSING)
            # Trivial read: fail loudly if the sessions table is unreadable.
            await cur.execute("SELECT 1 FROM accounts_sessions LIMIT 0")

    def readiness_targets(self) -> tuple[ReadinessTarget, ReadinessTarget]:
        # Both backing stores: the plugin's own Postgres and the injected Redis.
        return (
            ReadinessTarget("accounts", PostgresClient, accounts_settings().pg),
            ReadinessTarget("accounts", RedisClient, self.settings.redis),
        )


# One call registers the factory in both the accounts and identity registries
# under the same name — an accounts provider answers its own session tokens.
register_accounts_provider("accounts-postgres", PostgresAccountsProvider)
