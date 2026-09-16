"""Login/logout operations fanning out over the accounts-provider registry.

- ``login_methods`` aggregates every registered accounts provider's declared
  ``LoginMethod`` metadata plus the platform ``needs_setup`` fact and what the
  setup door can attach, so a generic login screen can render without knowing
  which providers are installed.
- ``logout`` revokes the caller's session by fanning ``revoke_session`` out over
  every presented credential candidate and every registered accounts provider;
  the first ``True`` wins, and no match is a loud 404 (:class:`NotFoundError`).
- ``exchange_claim_token`` burns a one-time claim token and returns the raw API key it
  carried — the public QR-onboarding exchange leg, ``authority_changing`` so it never
  projects as an MCP tool.

Both operations fan out over the CURRENT epoch's live accounts-provider instances —
the ones the epoch build eagerly instantiated and recorded — rather than
re-instantiating a provider per call. ``needs_setup`` reads live so the setup screen
disappears the moment the owner exists. Provider errors propagate (loud, never a
silently empty methods list or a silent logout no-op).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from tai42_contract.access_control import get_current_user_id
from tai42_contract.accounts import AccountsProvider, LoginAttachingProvider
from tai42_contract.accounts.models import InviteCredential, PasswordCredential

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.claim_links import ClaimLinkError
from tai42_skeleton.access_control.claim_links import exchange_claim_token as _exchange_claim_token
from tai42_skeleton.operations import NotFoundError, operation
from tai42_skeleton.operations.response_models_group_c import (
    ClaimExchangeResult,
    LoginMethodsListing,
    LogoutResult,
)

logger = logging.getLogger(__name__)


def _active_accounts_providers() -> list[AccountsProvider]:
    """The CURRENT epoch's live accounts-provider instances, name-sorted for a deterministic aggregate.

    Filters the epoch's recorded identity providers to the accounts ones (an accounts
    provider IS an identity provider); reads through the serving core so a build in flight
    resolves the epoch being built, else the live one.
    """
    from tai42_skeleton.app.instance import app

    recorded = app._serving_core.active_auth_providers
    return [p for _name, p in sorted(recorded.items()) if isinstance(p, AccountsProvider)]


class ClaimExchange(BaseModel):
    """Exchange a one-time claim token for its API key."""

    token: str = Field(min_length=1)


# The login credential kinds the setup door can attach — the closed
# :data:`~tai42_contract.accounts.models.LoginCredential` union, named here so the
# ``setup_login.kinds`` the client reads never drifts from the union the door accepts.
_SETUP_LOGIN_KINDS = [PasswordCredential.model_fields["kind"].default, InviteCredential.model_fields["kind"].default]


@operation(summary="List available login methods", tags=["login"], response_model=LoginMethodsListing)
async def login_methods() -> dict:
    """Aggregate every registered accounts provider's login methods + the setup state.

    ``authed=False`` is OpenAPI truth-telling only; runtime public-ness comes from the
    always-public ``/api/login`` prefix. Each method is serialized with
    ``model_dump(exclude_none=True)`` so a ``None``-valued optional (icon/autocomplete)
    is OMITTED, never ``null`` (the Studio's zod schemas accept absent but reject
    ``null``). ``needs_setup`` is the one platform fact "no principal exists"; ``setup_login``
    names what the setup door can attach (``kinds`` from a configured login-attaching
    provider, ``null`` when none). Provider errors propagate (loud, never a silently empty
    methods list).
    """
    methods: list[dict] = []
    has_login_attaching = False
    for provider in _active_accounts_providers():
        methods.extend(method.model_dump(exclude_none=True) for method in provider.login_methods())
        if isinstance(provider, LoginAttachingProvider):
            has_login_attaching = True
    needs_setup = not await management.any_principal_exists()
    setup_login = {"kinds": list(_SETUP_LOGIN_KINDS)} if has_login_attaching else None
    return {"methods": methods, "needs_setup": needs_setup, "setup_login": setup_login}


@operation(
    summary="Exchange a one-time claim token for its API key",
    tags=["login"],
    authority_changing=True,
    errors=[NotFoundError],
    request_model=ClaimExchange,
    response_model=ClaimExchangeResult,
)
async def exchange_claim_token(token: str) -> dict:
    """Burn a one-time claim token and return the raw API key it carried — the public exchange leg.

    The public exchange leg of QR onboarding (``authed=False``; runtime public-ness comes
    from the always-public ``/api/login`` prefix).

    The claim record is single-use: a used / unknown / expired token all answer the SAME
    404 (no oracle distinguishing them). The handed-out key is guaranteed not-REVOKED
    (re-validated at exchange), NOT owner-alive — owner-death is enforced by the gate on
    every request. Response mirrors the ``loginResult`` wire shape:
    ``{"data": {"token": <raw key>, "user_id": ...}}``.

    ``authority_changing=True`` keeps this OFF the default MCP tool surface: a
    credential-exchange login door is not an agent tool (it sits outside the
    ``/api/auth/*`` prefix, so the flag is what excludes it, not the prefix).
    """
    try:
        return await _exchange_claim_token(token)
    except ClaimLinkError as exc:
        # The store's only exchange failure is the uniform 404 miss.
        raise NotFoundError(exc.message) from exc


@operation(
    summary="Log out the current session",
    tags=["access-control"],
    destructive=True,
    errors=[NotFoundError],
    response_model=LogoutResult,
)
async def logout(candidates: list[str]) -> dict:
    """Revoke the caller's session by fanning ``revoke_session`` out over every candidate and provider.

    Fans out over every presented credential candidate and every registered accounts
    provider.

    Iterating all candidates is required: a client may present a stale value in
    ``Authorization`` alongside its live session in ``X-Api-Key``, so checking only the
    first would leave that session alive. The FIRST ``True`` → ``{"revoked": true}``.
    Only when EVERY candidate against EVERY provider returns ``False`` → 404 (an
    ``sk-`` API key cannot "log out" — loud, not a silent no-op), with a server-side
    log naming the caller so probing/replay stays visible. Provider errors propagate
    (fail closed).
    """
    providers = _active_accounts_providers()

    # Tokens outer, providers inner (registry order): the first provider to own any
    # presented candidate revokes it and wins.
    for token in candidates:
        for provider in providers:
            if await provider.revoke_session(token):
                return {"revoked": True}

    logger.info(
        "access_control: logout by %s matched no accounts provider (presented credential is not a revocable session)",
        get_current_user_id(),
    )
    raise NotFoundError("Not a revocable session")
