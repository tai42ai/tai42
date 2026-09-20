"""``PostgresAccountsProvider`` — session-token validation, login methods, login attachment.

Storage is the plugin's own Postgres schema; login throttling lives in the injected
Redis. Registered at import. As a :class:`LoginAttachingProvider` it can attach the
owner's login for the platform setup door.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from tai42_contract.access_control.identity import AuthIdentity, ReadinessTarget
from tai42_contract.accounts import (
    FormField,
    FormMethod,
    LoginAttachError,
    LoginAttachingProvider,
    LoginAttachment,
    LoginCredential,
    LoginMethod,
    register_accounts_provider,
)
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.db import component_store_settings

from tai42_accounts_postgres import service
from tai42_accounts_postgres.db import COMPONENT, assert_accounts_schema_applied
from tai42_accounts_postgres.hashing import hash_password_async
from tai42_accounts_postgres.settings import accounts_settings

if TYPE_CHECKING:
    from tai42_contract.accounts import AccountsProviderSettings

logger = logging.getLogger(__name__)

# ``last_seen_at`` is written only when more than this stale, sparing a PG UPDATE
# per request on a busy session.
_TOUCH_THROTTLE_SECONDS = 60


class PostgresAccountsProvider(LoginAttachingProvider):
    """Validate sessions, declare login methods, and attach the owner's login at setup."""

    def __init__(self, settings: AccountsProviderSettings) -> None:
        """Bind the injected ``settings`` (Postgres + Redis connection) to this provider instance."""
        # The injected settings live on the INSTANCE; the epoch records this provider so
        # the routes resolve it through the accounts facet — no module holder to leak on
        # a failed build.
        self.settings = settings

    async def validate_token(self, token: str) -> AuthIdentity | None:
        """The :class:`AuthIdentity` for a live session ``token``, or ``None`` when it is not ours or invalid.

        Fails CLOSED: a store error raises rather than reading as an invalid credential. An
        expired or idle-timed-out session is deleted and reads as ``None``.
        """
        # Fast-reject non-session tokens without a DB hit so resolution moves on.
        if not token.startswith(service.SESSION_TOKEN_PREFIX):
            return None

        token_hash = service.token_hash(token)
        store = service.sessions_store()
        try:
            row = await store.resolve(token_hash)
        except Exception:
            # Fail closed: RAISE, never return None (which reads as invalid-credential).
            logger.exception("accounts: session resolve failed")
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
        """The login methods the sign-in UI renders: password sign-in and invite acceptance."""
        # Static config-derived metadata. Submit paths resolve through the login
        # router's mount base (captured at its import) so an operator remap of the
        # route base moves them too.
        from tai42_accounts_postgres.routes_login import submit_path

        return [
            FormMethod(
                id="password",
                title="Sign in",
                purpose="login",
                fields=[
                    FormField(name="email", label="Email", autocomplete="email"),
                    FormField(name="password", label="Password", secret=True, autocomplete="current-password"),
                ],
                submit_path=submit_path("/password"),
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
                submit_path=submit_path("/invite/accept"),
            ),
        ]

    async def has_login(self, user_id: str) -> bool:
        """Whether an ``accounts_users`` login row exists for principal ``user_id``.

        The principals door reads this to route a disable/delete: a human whose
        password login lives here is managed through this provider's users door,
        not the principals door. A store error propagates (fail closed).
        """
        return await service.users_store().get_by_user_id(user_id) is not None

    async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:
        """Attach the owner's interactive login to the EXISTING principal ``user_id``.

        Called by the platform setup door once the owner principal is created. A
        :class:`PasswordCredential` sets the password now and returns ``attached=True``;
        an :class:`InviteCredential` leaves the password unset and returns the one-time
        invite link on the attachment. Either way the ``accounts_users`` login row is
        created for the owner, whose role mirrors the owner's admin principal. A too-short
        password raises :class:`~tai42_contract.accounts.errors.LoginAttachError`; a login
        already existing for the principal or a taken email raises
        :class:`~tai42_contract.accounts.errors.LoginConflictError` (through the store's
        :class:`LoginExistsError`/:class:`EmailTakenError`) — the setup door surfaces the
        failure and stays retriable.
        """
        email = service.normalize_email(credential.email)
        store = service.users_store()
        if credential.kind == "password":
            if len(credential.password) < service.PASSWORD_MIN_LENGTH:
                raise LoginAttachError(f"password must be at least {service.PASSWORD_MIN_LENGTH} characters")
            password_hash = await hash_password_async(credential.password)
            await store.create_login(user_id, email, service.ADMIN_ROLE, password_hash)
            return LoginAttachment(attached=True)

        # An invite credential: create the password-less login row, then mint the
        # one-time invite. If the invite mint fails, drop the just-created row so the
        # owner attach stays re-runnable rather than leaving a login with no way in.
        await store.create_login(user_id, email, service.ADMIN_ROLE)
        try:
            raw_invite = service.new_invite_token()
            expires_at = datetime.now(UTC) + timedelta(seconds=accounts_settings().invite_ttl_seconds)
            await service.invites_store().create(service.token_hash(raw_invite), user_id, expires_at)
        except Exception:
            await store.delete(user_id)
            raise
        return LoginAttachment(
            attached=False,
            invite_token=raw_invite,
            login_path=service.invite_login_path(raw_invite),
        )

    async def revoke_session(self, token: str) -> bool:
        """Delete the session for ``token``; returns ``False`` when the token is not ours to revoke."""
        if not token.startswith(service.SESSION_TOKEN_PREFIX):
            # Not ours — the logout dispatcher moves on.
            return False
        return await service.sessions_store().delete(service.token_hash(token))

    async def healthcheck(self) -> None:
        """Boot-time gate: assert the plugin's schema chain is fully applied."""
        await assert_accounts_schema_applied()

    def readiness_targets(self) -> tuple[ReadinessTarget, ReadinessTarget]:
        """The provider's backing stores probed by the readiness check: its Postgres and the injected Redis."""
        # Both backing stores: the plugin's own Postgres and the injected Redis.
        return (
            ReadinessTarget("accounts", PostgresClient, component_store_settings(COMPONENT)),
            ReadinessTarget("accounts", RedisClient, self.settings.redis),
        )


# One call registers the factory in both the accounts and identity registries
# under the same name — an accounts provider answers its own session tokens.
register_accounts_provider("accounts-postgres", PostgresAccountsProvider)
