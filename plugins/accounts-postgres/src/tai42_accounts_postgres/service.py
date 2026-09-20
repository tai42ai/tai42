"""Shared plumbing for the provider and both route modules.

Two concerns: resolving the CURRENT epoch's provider settings (``.admin`` /
``.redis``) from the live provider instance the epoch recorded — no module holder, so
a failed epoch build never leaks; and token/id minting and email normalization
(tokens are distinctly prefixed, stored only as SHA-256).
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from tai42_kit.db import component_store_settings
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_accounts_postgres.db import COMPONENT
from tai42_accounts_postgres.settings import accounts_settings
from tai42_accounts_postgres.stores import InvitesStore, SessionsStore, UsersStore, new_user_id

__all__ = ["new_user_id"]

if TYPE_CHECKING:
    from tai42_contract.accounts import AccountsProviderSettings

    from tai42_accounts_postgres.provider import PostgresAccountsProvider

logger = logging.getLogger(__name__)

# Distinct prefixes let validate_token fast-reject non-session tokens without a DB hit.
SESSION_TOKEN_PREFIX = "tai-sess-"  # noqa: S105 constant identifier, not a secret value
INVITE_TOKEN_PREFIX = "tai-inv-"  # noqa: S105 constant identifier, not a secret value

# ``"admin"`` is a reserved, non-renamable, non-deletable role name, so admin-ness
# is exactly ``role == "admin"`` — the basis the last-admin guard keys on.
ADMIN_ROLE = "admin"

# Minimum password length (no composition rules — NIST 800-63B stance).
PASSWORD_MIN_LENGTH = 10


# -- live provider resolution (per epoch, no module holder) ---------------------
# The injected settings (``.admin`` / ``.redis``) live on the epoch's provider
# INSTANCE. Route handlers and the record helpers resolve the CURRENT epoch's instance
# through the app's accounts facet, so a failed epoch build's provider is discarded
# with its core and never leaks into the live epoch.


def provider_settings() -> AccountsProviderSettings:
    """The injected settings of the CURRENT epoch's provider instance.

    Raises when no provider is active (the accounts kind is disabled / not
    configured).
    """
    from tai42_contract.app import tai42_app

    provider = tai42_app.accounts.active_provider("accounts-postgres")
    if provider is None:
        raise RuntimeError(
            "tai42-accounts-postgres has no active provider: access control is disabled or "
            "'accounts-postgres' is absent from ACCESS_CONTROL_AUTH_PROVIDERS."
        )
    return cast("PostgresAccountsProvider", provider).settings


def provider_settings_populated() -> bool:
    """Whether an accounts-postgres provider is active this epoch — the boot guard's input."""
    from tai42_contract.app import tai42_app

    return tai42_app.accounts.active_provider("accounts-postgres") is not None


# -- store accessors (tests swap these for in-memory fakes) ---------------------


def users_store() -> UsersStore:
    return UsersStore(component_store_settings(COMPONENT))


def sessions_store() -> SessionsStore:
    return SessionsStore(component_store_settings(COMPONENT))


def invites_store() -> InvitesStore:
    return InvitesStore(component_store_settings(COMPONENT))


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


def too_many_attempts_message(retry_after: int) -> str:
    """The informative 429 body text, surfaced verbatim in the throttled response."""
    minutes = max(1, math.ceil(retry_after / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many attempts — try again in {minutes} {unit}"
