"""Response models for the login operations: login methods, claim exchange and logout."""

from __future__ import annotations

from pydantic import BaseModel
from tai42_contract.accounts.models import LoginCredentialKind, LoginMethod


class SetupLogin(BaseModel):
    """What the setup door can attach as the owner's interactive login.

    ``kinds`` are the login-credential kinds a configured login-attaching provider accepts
    (e.g. ``["password", "invite"]``); the client decides from this whether to show the
    owner-login fields.
    """

    kinds: list[LoginCredentialKind]


class LoginMethodsListing(BaseModel):
    """The aggregated login surface.

    Each method is dumped ``exclude_none`` so an unset optional (icon/autocomplete) is
    OMITTED, never ``null``. ``needs_setup`` is the one platform fact "no principal exists"
    (the login screen renders the setup entry instead of sign-in forms while true).
    ``setup_login`` names what the setup door can attach, or ``null`` when no login-attaching
    provider is configured. ``claim_fragment_key`` is the key a claim link carries its token
    under in the login page's fragment (``HOSTED_UI_CLAIM_FRAGMENT_KEY``), so a client cuts the
    token out of a pasted link.
    """

    methods: list[LoginMethod]
    needs_setup: bool
    setup_login: SetupLogin | None = None
    claim_fragment_key: str


class ClaimExchangeResult(BaseModel):
    """The one-time claim-token exchange result.

    ``token`` is a raw, one-time API key — a SECRET; it must never be logged or
    rendered where a key leaks.
    """

    token: str
    user_id: str


class LogoutResult(BaseModel):
    """The logout confirmation — success-only (a non-revocable session is a 404)."""

    revoked: bool
