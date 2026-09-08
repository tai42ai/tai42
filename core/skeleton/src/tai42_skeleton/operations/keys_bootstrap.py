"""The first-key bootstrap operation — mint the first admin api key on a fresh
deployment behind the secure-by-default token gate.

A deployment with access control ON and the api-key identity provider has no
authenticated door to mint its first credential: ``POST /api/auth/api-keys`` is itself
authed. This operation is the one-shot public door that closes that gap. It is gated by
a boot-time token (see :mod:`tai42_skeleton.access_control.bootstrap`), refuses once any
key exists, and mints a condition-free ``["*"]`` admin key through the same
:mod:`~tai42_skeleton.access_control.management` mint the authed door uses — so the key
lands in both storage homes (the provider's identity record and the policy store)
exactly as a normal mint does.

``authority_changing`` keeps it off the default MCP tool surface: an unauthenticated
key-minting door is never an agent tool.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.bootstrap import (
    BootstrapContended,
    bootstrap_mint_lock,
    bootstrap_throttle_locked,
    clear_bootstrap_failures,
    record_bootstrap_failure,
    verify_bootstrap_token,
)
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.operations import ConflictError, ForbiddenError, NotSupportedError, operation

logger = logging.getLogger(__name__)


class BootstrapKeyBody(BaseModel):
    """First-admin-key creation for ``POST /api/keys/bootstrap``."""

    user_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    bootstrap_token: str = ""


class BootstrapKeyResult(BaseModel):
    """The one-time bootstrap result: the raw ``sk-…`` admin key and its user id."""

    token: str
    user_id: str


@operation(
    summary="Mint the first admin api key",
    tags=["access-control"],
    authority_changing=True,
    errors=[ForbiddenError, ConflictError, NotSupportedError],
    request_model=BootstrapKeyBody,
    response_model=BootstrapKeyResult,
)
async def bootstrap_admin_key(
    user_id: str, description: str, bootstrap_token: str = "", client_ip: str = "unknown"
) -> dict:
    """Mint the first admin api key behind the secure-by-default token gate.

    Only meaningful on an access-controlled install: with the gate OFF there is nothing to
    protect, so the door refuses loudly with a 501 rather than minting. The per-IP backoff
    is consulted BEFORE the token, so a wrong-token flood escalates a lockout that turns
    further attempts away without ever comparing; a wrong/absent token is a generic 403 (no
    oracle for the initialized state). The existence-check-and-mint runs under one mutex, so
    two concurrent bootstraps can never both mint — the loser gets a 409. On a passing gate
    a condition-free ``["*"]`` admin key — the ``is_admin_policy`` discriminator — is minted
    through the shared mint and returned ONCE.

    Response mirrors the login result wire shape: ``{"data": {"token": <raw key>,
    "user_id": ...}}``.
    """
    if not access_control_settings().enable:
        raise NotSupportedError(
            "the first-key bootstrap door serves access-controlled installs only; it is disabled "
            "while ACCESS_CONTROL_ENABLE is off (with the gate off there is no key to mint)"
        )

    if await bootstrap_throttle_locked(client_ip):
        logger.warning("access_control: first-key bootstrap throttled for ip=%s", client_ip)
        raise ForbiddenError("Forbidden")

    if not await verify_bootstrap_token(bootstrap_token):
        await record_bootstrap_failure(client_ip)
        logger.warning("access_control: first-key bootstrap token mismatch/absent from ip=%s", client_ip)
        raise ForbiddenError("Forbidden")

    if not any(mintable for _name, mintable in management.provider_capabilities()):
        raise NotSupportedError(
            "no configured identity provider can mint api keys; the first-key bootstrap door "
            "requires a key-minting provider"
        )

    try:
        async with bootstrap_mint_lock():
            if await management.get_all_existing_tokens_payload():
                raise ConflictError("Already initialized")
            raw_key, _body, _fingerprint = await management.add_user_api_key(user_id, description, ["*"])
    except BootstrapContended as exc:
        raise ConflictError("Already initialized") from exc

    await clear_bootstrap_failures(client_ip)
    return {"token": raw_key, "user_id": user_id}
