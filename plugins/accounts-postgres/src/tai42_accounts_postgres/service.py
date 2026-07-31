"""Shared plumbing for the provider and both route modules.

Three concerns: the module-level settings holder (populated at provider
``__init__``, read back by handlers, fail-loud when unset); token/id minting and
email normalization (tokens are distinctly prefixed, stored only as SHA-256); and
the first-owner bootstrap token (fixed once at startup via SET NX, read per
request — never generated per call).
"""

from __future__ import annotations

import logging
import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from tai42_kit.clients import RedisConnectionSettings, client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_accounts_postgres.settings import accounts_settings
from tai42_accounts_postgres.stores import InvitesStore, SessionsStore, UsersStore, new_user_id

__all__ = ["new_user_id"]

if TYPE_CHECKING:
    from tai42_contract.accounts import AccountsProviderSettings

logger = logging.getLogger(__name__)

# Distinct prefixes let validate_token fast-reject non-session tokens without a DB hit.
SESSION_TOKEN_PREFIX = "tai-sess-"
INVITE_TOKEN_PREFIX = "tai-inv-"

# ``"admin"`` is a reserved, non-renamable, non-deletable role name, so admin-ness
# is exactly ``role == "admin"`` — the basis the last-admin guard keys on.
ADMIN_ROLE = "admin"

# Minimum password length (no composition rules — NIST 800-63B stance).
PASSWORD_MIN_LENGTH = 10


# -- settings holder (populated at provider __init__) ---------------------------

_provider_settings: AccountsProviderSettings | None = None


def set_provider_settings(settings: AccountsProviderSettings) -> None:
    """Store the injected settings so route handlers can reach ``.admin`` /
    ``.redis``. Called from the provider's ``__init__`` — the only writer."""
    global _provider_settings
    _provider_settings = settings


def provider_settings() -> AccountsProviderSettings:
    """Return the injected settings; RAISE if the holder was never populated
    (the provider was never instantiated — access control is disabled)."""
    if _provider_settings is None:
        raise RuntimeError(
            "tai42-accounts-postgres settings holder is unpopulated: the provider was never "
            "instantiated. The accounts kind requires ACCESS_CONTROL_ENABLE=true and "
            "'accounts-postgres' present in ACCESS_CONTROL_AUTH_PROVIDERS."
        )
    return _provider_settings


def provider_settings_populated() -> bool:
    """Whether the holder has been populated — the boot guard's input."""
    return _provider_settings is not None


def reset_provider_settings() -> None:
    """Clear the holder (test isolation)."""
    global _provider_settings
    _provider_settings = None


# -- store accessors (tests swap these for in-memory fakes) ---------------------


def users_store() -> UsersStore:
    return UsersStore(accounts_settings().pg)


def sessions_store() -> SessionsStore:
    return SessionsStore(accounts_settings().pg)


def invites_store() -> InvitesStore:
    return InvitesStore(accounts_settings().pg)


# -- minting / normalization ----------------------------------------------------


def normalize_email(email: str) -> str:
    """The stored form: trimmed and lowercased (uniqueness is on this value)."""
    return email.strip().lower()


def new_session_token() -> str:
    return f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def new_invite_token() -> str:
    return f"{INVITE_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    """SHA-256 at rest — the same primitive api keys use, uniform across types."""
    return hash_api_key(token)


async def mint_session(user_id: str) -> str:
    """Create a session for ``user_id`` and return the RAW token (shown once)."""
    settings = accounts_settings()
    raw = new_session_token()
    absolute_expires_at = datetime.now(UTC) + timedelta(seconds=settings.session_absolute_seconds)
    await sessions_store().create(token_hash(raw), user_id, absolute_expires_at)
    return raw


def invite_login_path(raw_invite_token: str) -> str:
    """The origin-relative Studio path an admin hands to the invitee."""
    return f"/login?invite={raw_invite_token}"


async def apply_role_compensated(user_id: str, role: str, cleanup: Callable[[], Awaitable[None]]) -> None:
    """Apply a role template through the injected services, compensating on failure.

    If ``apply_role`` raises after the user row was written, run ``cleanup`` before
    propagating so the operation stays re-runnable. If the cleanup itself fails,
    both errors are preserved and the message names the residual row for manual
    deletion — the stuck state is never reached silently.
    """
    admin = provider_settings().admin
    try:
        await admin.apply_role(user_id, role)
    except Exception as apply_exc:
        try:
            await cleanup()
        except Exception:
            raise RuntimeError(
                f"apply_role for {user_id!r} failed and the compensating cleanup also failed; "
                f"a residual accounts_users row {user_id!r} remains — delete it manually"
            ) from apply_exc
        raise


def too_many_attempts_message(retry_after: int) -> str:
    """The informative 429 body text, surfaced verbatim in the throttled response."""
    minutes = max(1, math.ceil(retry_after / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many attempts — try again in {minutes} {unit}"


# -- bootstrap token (secure-by-default gate) -----------------------------------


def _redis(redis_settings: Any) -> RedisConnectionSettings:
    # Bridge the contract's ``Any`` redis to kit's nominal settings type.
    return cast("RedisConnectionSettings", redis_settings)


def bootstrap_token_redis_key() -> str:
    """The per-deployment-namespaced key the shared auto-token lives under."""
    return f"{accounts_settings().key_prefix}:acc:bootstrap:token"


async def ensure_bootstrap_token(redis_settings: Any) -> None:
    """Fix the shared auto-token once at startup (no-op when no token is needed).

    Each process attempts ``SET key <fresh> NX``; the winner fixes the effective
    token and is the only process that logs it. Generated once at boot, never per
    request.
    """
    settings = accounts_settings()
    if settings.bootstrap_open or settings.bootstrap_token is not None:
        return
    key = bootstrap_token_redis_key()
    candidate = secrets.token_urlsafe(32)
    async with client_ctx(RedisClient, _redis(redis_settings)) as r:
        won = await r.set(key, candidate, nx=True)
    if won:
        logger.info(
            "first-owner bootstrap token: %s — paste it into the owner-creation form; "
            "only someone who can read this log can create the admin owner",
            candidate,
        )


async def resolve_bootstrap_token(redis_settings: Any) -> str:
    """The effective bootstrap token when the gate is active — read, never generated.

    An operator-set token is returned directly; otherwise the shared auto-token is
    read from Redis. Its absence while the gate is active RAISES rather than
    silently opening the front door.
    """
    settings = accounts_settings()
    if settings.bootstrap_token is not None:
        return settings.bootstrap_token.get_secret_value()
    key = bootstrap_token_redis_key()
    async with client_ctx(RedisClient, _redis(redis_settings)) as r:
        stored = await r.get(key)
    if not stored:
        raise RuntimeError(
            "bootstrap token invariant breach: the auto-generated first-owner token is absent "
            "from Redis while the gate is active (Redis may have been flushed, or this instance "
            "restarted mid-life); restart the deployment to regenerate it"
        )
    # decode_responses yields a str; decode defensively against redis-py's ResponseT.
    return stored.decode() if isinstance(stored, bytes) else stored
