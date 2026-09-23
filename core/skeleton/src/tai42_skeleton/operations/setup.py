"""The one-shot setup operation — initialize a deployment behind the setup-token gate.

A fresh deployment with access control ON has no authenticated door to create its first
credential. ``POST /api/setup`` is that public, one-shot door: gated by a boot-time token
(see :mod:`tai42_skeleton.access_control.setup_gate`), it refuses once any principal
exists, and it initializes the deployment atomically — it creates the OWNER principal
(``kind=human``, admin role), mints the owner's first key owned by the owner, and, when a
configured accounts provider can attach an interactive login, attaches the owner's login
from the request. The raw key is returned exactly once.

``authority_changing`` keeps it off the default MCP tool surface: an unauthenticated
deployment-initializing door is never an agent tool.
"""

from __future__ import annotations

import logging
from secrets import token_urlsafe

from tai42_contract.accounts import LoginAttachingProvider
from tai42_contract.accounts.errors import LoginAttachError, LoginConflictError
from tai42_contract.accounts.models import LoginAttachment, LoginCredential
from tai42_contract.setup import SetupRequest, SetupResult

from tai42_skeleton.access_control import management, roles
from tai42_skeleton.access_control.roles import RESERVED_ADMIN_ROLE
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.access_control.setup_gate import (
    SetupContendedError,
    clear_setup_failures,
    record_setup_failure,
    setup_lock,
    setup_throttle_locked,
    setup_unserviceable_reason,
    verify_setup_token,
)
from tai42_skeleton.access_control.store import access_control_store
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotSupportedError,
    operation,
)

logger = logging.getLogger(__name__)


def _active_login_attaching_provider() -> LoginAttachingProvider | None:
    """The first active accounts provider that can attach an interactive login, or ``None``.

    Reads the CURRENT epoch's live accounts-provider instances, name-sorted for a
    deterministic choice. A provider whose login lives at an external issuer (OIDC) does
    not implement :class:`LoginAttachingProvider`, so a keys-only or OIDC-only deployment
    yields ``None`` and the owner's login is reported as not attached.
    """
    from tai42_skeleton.app.instance import app

    recorded = app._serving_core.active_auth_providers
    for _name, provider in sorted(recorded.items()):
        if isinstance(provider, LoginAttachingProvider):
            return provider
    return None


async def _compensate(owner_user_id: str, key_user_id: str, *, key_minted: bool) -> None:
    """Best-effort teardown after a setup step failed past the owner-principal create.

    Deletes the freshly-minted key (if any), the owner's policy row, and the owner's
    principal row so setup stays retriable. A teardown fault is logged loudly (the original
    failure is re-raised by the caller); it never masks the initializing error.
    """
    store = access_control_store()
    try:
        if key_minted:
            await management.revoke_api_key(key_user_id)
        await store.delete_policy(owner_user_id)
        await store.delete_principal(owner_user_id)
        await management.bump_policy_version()
    except Exception:
        logger.exception("setup: compensation could not fully tear down owner %s", owner_user_id)


@operation(
    summary="Initialize the deployment (owner + first key)",
    tags=["access-control"],
    authority_changing=True,
    errors=[BadRequestError, ForbiddenError, ConflictError, NotSupportedError],
    request_model=SetupRequest,
    response_model=SetupResult,
)
async def setup_deployment(
    setup_token: str = "",
    owner_user_id: str | None = None,
    owner_display_name: str = "",
    key_user_id: str | None = None,
    key_description: str = "owner key",
    login: LoginCredential | None = None,
    client_ip: str = "unknown",
) -> dict:
    """Initialize the deployment once behind the secure-by-default setup-token gate.

    Only meaningful on a serviceable access-controlled install: it refuses loudly with a
    501 when the door cannot initialize — access control off, no configured key-minting
    identity provider, or the access-control Redis unset. The per-IP backoff is consulted
    BEFORE the token, so a wrong-token flood escalates a lockout that turns further attempts away
    without ever comparing; a wrong/absent token is a generic 403 (no oracle for the
    initialized state). Under one mint mutex: 409 "Already initialized" when ANY principal
    exists; else the owner principal (``kind=human``) is created, granted the admin role,
    its first key (owned by the owner) is minted and returned ONCE, and — when a configured
    accounts provider can attach a login and the request carries one — the owner's login is
    attached (a password set now, or an invite whose one-time link is returned). A failure
    past the owner create is compensated so setup stays retriable; a correctable login
    credential is compensated then mapped to the caller (a too-short password → 400, a
    login/email collision → 409) rather than surfaced as a 500.
    """
    unserviceable = setup_unserviceable_reason(access_control_settings())
    if unserviceable is not None:
        raise NotSupportedError(unserviceable)

    if await setup_throttle_locked(client_ip):
        logger.warning("setup: throttled for ip=%s", client_ip)
        raise ForbiddenError("Forbidden")

    if not await verify_setup_token(setup_token):
        await record_setup_failure(client_ip)
        logger.warning("setup: token mismatch/absent from ip=%s", client_ip)
        raise ForbiddenError("Forbidden")

    owner_id = owner_user_id or f"usr-{token_urlsafe(8)}"
    key_id = key_user_id or f"{owner_id}-key"

    try:
        async with setup_lock():
            owner = await access_control_store().create_first_principal(owner_id, "human", owner_display_name)
            if owner is None:
                # A principal already exists — the deployment is initialized. Generic text, no oracle.
                raise ConflictError("Already initialized")
            key_minted = False
            try:
                await roles.apply_role(owner_id, RESERVED_ADMIN_ROLE)
                raw_key, _body, key_fingerprint = await management.add_user_api_key(
                    key_id, key_description, ["*"], owner_user_id=owner_id
                )
                key_minted = True
                attachment = await _attach_owner_login(owner_id, login)
            except LoginConflictError as exc:
                # A login/email collision the operator can resolve: compensate so setup
                # stays retriable, then answer 409 (not a raw 500).
                await _compensate(owner_id, key_id, key_minted=key_minted)
                raise ConflictError(str(exc)) from exc
            except LoginAttachError as exc:
                # A correctable-input login failure (a too-short password): compensate,
                # then answer 400 so the caller can retry with a valid credential.
                await _compensate(owner_id, key_id, key_minted=key_minted)
                raise BadRequestError(str(exc)) from exc
            except Exception:
                await _compensate(owner_id, key_id, key_minted=key_minted)
                raise
    except SetupContendedError as exc:
        raise ConflictError("Already initialized") from exc

    await clear_setup_failures(client_ip)
    return {
        "owner_user_id": owner_id,
        "key_user_id": key_id,
        "api_key": raw_key,
        "key_fingerprint": key_fingerprint,
        "login_attached": attachment.attached,
        "invite_token": attachment.invite_token,
        "login_path": attachment.login_path,
    }


async def _attach_owner_login(owner_id: str, login: LoginCredential | None) -> LoginAttachment:
    """Attach the owner's login through the first active login-attaching provider, or report not-attached.

    Returns the provider's :class:`~tai42_contract.accounts.models.LoginAttachment` when a
    login is requested and a provider can attach it; otherwise a not-attached attachment
    (a keys-only or OIDC-only deployment, or a request with no ``login``).
    """
    if login is None:
        return LoginAttachment(attached=False)
    provider = _active_login_attaching_provider()
    if provider is None:
        return LoginAttachment(attached=False)
    return await provider.attach_login(owner_id, credential=login)
