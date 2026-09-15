"""Key CRUD + claim-link doors: list payloads, create/edit/modify-scopes/revoke, claim link."""

from __future__ import annotations

from typing import Any

from tai42_contract.template import TemplatedText

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.claim_links import ClaimLinkError
from tai42_skeleton.access_control.claim_links import create_claim_link as _create_claim_link
from tai42_skeleton.operations import BadRequestError, ForbiddenError, NotFoundError, NotSupportedError, operation
from tai42_skeleton.operations._authority import owner_of, require_owned_by_caller
from tai42_skeleton.operations.response_models_group_a import (
    ApiKeyCreateResult,
    ClaimLinkResult,
    RevokeAck,
    ScopesUpdateAck,
    TokenPayloadList,
    UserUpdateAck,
)

from . import ownership
from .models import _DISABLED_CODE, _DISABLED_MESSAGE, ApiKeyCreate, ApiKeyEdit, ClaimLinkCreate, KeyScopesModify


@operation(summary="List api-key token payloads", tags=["access-control"], response_model=TokenPayloadList)
async def list_tokens_payload() -> list[dict[str, Any]]:
    """Every provisioned key's identity + policy (NEVER key material).

    Non-admin callers see ONLY the keys they own (management/listing owner home); admin sees every key.
    """
    # OFF: access control disabled → no provisioned keys; the honest empty list,
    # never a store read under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        return []
    caller = await _pkg.resolve_caller()
    payload = await management.get_all_existing_tokens_payload()
    if not caller.is_admin:
        payload = [p for p in payload if owner_of(p.get("policy_data")) == caller.caller_id]
    return payload


@operation(
    summary="Create an api key",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, ForbiddenError, NotSupportedError],
    request_model=ApiKeyCreate,
    response_model=ApiKeyCreateResult,
)
async def create_api_key(
    user_id: str,
    description: str,
    scopes: list[str],
    policy_data: dict[str, Any] | None,
    condition: TemplatedText | None,
    owner_user_id: str | None,
) -> dict[str, Any]:
    """Provision a key, returning ``{"api_key", "key_fingerprint"}``.

    The raw ``sk-…`` ``api_key`` is surfaced ONCE; ``key_fingerprint`` is the key's immutable per-mint
    identity a caller binds a hook against so the binding survives only this exact mint.
    """
    # OFF: access control disabled → refuse the mint with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    caller = await _pkg.resolve_caller()
    # An owned key cannot mint keys — ownership is exactly one level deep.
    if caller.owner_claim is not None:
        raise ForbiddenError("an owned API key may not mint API keys")
    if not caller.is_admin:
        # Non-admin: force self-ownership (reject an explicit different owner, never
        # silently overwrite) and cap the grant to the caller's own scopes.
        if owner_user_id is not None and owner_user_id != caller.caller_id:
            raise ForbiddenError("a non-admin caller may only create keys owned by itself")
        owner_user_id = caller.caller_id
        ownership._check_scope_subset(caller, scopes)

    try:
        raw_key, committed_body, key_fingerprint = await management.add_user_api_key(
            user_id=user_id,
            description=description,
            scopes=scopes,
            policy_data=policy_data,
            condition=condition,
            owner_user_id=owner_user_id,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    # Store-first: ``add_user_api_key`` has written the policy to the enforced store
    # (the authority) and returned the exact body it committed. Bump the
    # cache-invalidation key immediately so enforcement follows, then record that body
    # as durable PG history. A store failure above raised before this, so neither the
    # bump nor the history is touched.
    await management.bump_policy_version()
    await _pkg._record_policy_version(user_id, committed_body)
    return {"api_key": raw_key, "key_fingerprint": key_fingerprint}


@operation(
    summary="Edit an api key",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=ApiKeyEdit,
    response_model=UserUpdateAck,
)
async def edit_api_key(user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """A PATCH-style partial edit: only the fields present in ``updates`` are overwritten.

    A field absent is preserved at its stored value, so saving a description or scope change never silently
    drops an authorization ``condition`` or ``policy_data`` gate. ``updates`` is the sparse set of present
    fields (a single dict rather than flattened params, so "field absent" stays distinct from "field is
    ``null``" — the partial-edit semantics a flat signature cannot express).
    """
    # OFF: access control disabled → refuse the edit with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    caller = await _pkg.resolve_caller()
    # Ownership + owner-claim immutability pre-checks (reads the stored body once when a
    # check needs it).
    await ownership._authorize_key_edit(caller, user_id, updates)

    try:
        updated = await management.edit_user_payload(user_id=user_id, **updates)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if not updated:
        raise NotFoundError(f"user not found: {user_id!r}")
    # Store-first: the edit has landed in the enforced store and returned the exact
    # committed body. Bump the cache key immediately so enforcement follows, then
    # record that body as a new PG version (see ``_record_policy_version``).
    await management.bump_policy_version()
    await _pkg._record_policy_version(user_id, updated)
    return {"user_id": user_id, "updated": True}


@operation(
    summary="Add/remove scopes on an api key",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=KeyScopesModify,
    response_model=ScopesUpdateAck,
)
async def modify_api_key_scopes(
    user_id: str, add: list[str] | None = None, remove: list[str] | None = None
) -> dict[str, Any]:
    """Add and/or remove named scopes on a key's stored scope set without replacing the whole set.

    The new set keeps the stored order, drops the removed scopes, then appends the additions in the given
    order. A plain read-merge-write with NO new locking: two simultaneous edits of one key can lose one
    (accepted for this surface).
    """
    add = add or []
    remove = remove or []
    # OFF: access control disabled → refuse the write with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    if not add and not remove:
        raise BadRequestError("nothing to change: provide scopes to add or remove")
    caller = await _pkg.resolve_caller()
    stored_body = await management.get_policy_body(user_id)
    if stored_body is None:
        raise NotFoundError(f"user not found: {user_id!r}")
    if not caller.is_admin:
        # A non-admin may edit only a key it owns, and may only ADD scopes ⊆ its own
        # (removing scopes never widens authority, so it carries no subset gate).
        if owner_of(stored_body.get("policy_data")) != caller.caller_id:
            raise ForbiddenError("you may only edit API keys you own")
        ownership._check_scope_subset(caller, add)
    stored_scopes = list(stored_body.get("scopes") or [])
    already_present = sorted(set(add) & set(stored_scopes))
    if already_present:
        raise BadRequestError(f"scopes already present on the key: {already_present}")
    absent = sorted(set(remove) - set(stored_scopes))
    if absent:
        raise NotFoundError(f"scopes not present on the key: {absent}")
    removed = set(remove)
    new = [scope for scope in stored_scopes if scope not in removed] + add
    # Store-first, byte-parallel to ``edit_api_key``: the edit lands in the enforced store
    # and returns the committed body, then the cache bump so enforcement follows, then the
    # durable version record (incl. the ``updated`` None → 404 re-check).
    try:
        updated = await management.edit_user_payload(user_id=user_id, scopes=new)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if not updated:
        raise NotFoundError(f"user not found: {user_id!r}")
    await management.bump_policy_version()
    await _pkg._record_policy_version(user_id, updated)
    return {"user_id": user_id, "updated": True, "scopes": new}


@operation(
    summary="Revoke an api key",
    tags=["access-control"],
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    response_model=RevokeAck,
)
async def revoke_api_key(user_id: str) -> dict[str, Any]:
    """Revoke a key (immediate: next request fails to auth).

    Deletes the key record, its enforced policy row, and its live context; the user's ``ac_policy`` version
    history is deliberately NOT touched (it belongs to the identity, so a key later re-created
    for the same ``user_id`` resumes that history).

    No version bump here, unlike every other mutation on this surface: revocation's
    cache-buster is atomic with the policy-row delete inside
    :func:`~tai42_skeleton.access_control.management.revoke_api_key`, so no fault in the
    steps behind it can leave the revoked key's authority live in a warm cache slot.
    """
    # OFF: access control disabled → refuse the revoke with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    caller = await _pkg.resolve_caller()
    if not caller.is_admin:
        # A non-admin may revoke only a key it owns.
        await require_owned_by_caller(caller, user_id)
    try:
        revoked = await management.revoke_api_key(user_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if not revoked:
        raise NotFoundError(f"user not found: {user_id!r}")
    return {"user_id": user_id, "revoked": True}


@operation(
    summary="Create a one-time claim link for an API key",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, ForbiddenError, NotSupportedError],
    request_model=ClaimLinkCreate,
    response_model=ClaimLinkResult,
)
async def create_claim_link(api_key: str, ttl_seconds: int | None) -> dict[str, Any]:
    """Mint a one-time claim link that carries ``api_key`` to another device (the QR onboarding leg).

    The submitted key is resolved through the gate's own verifier chain and the caller must own it
    (or be admin) per the module's ownership rule; the response returns the claim token ONCE plus a
    fragment-carrier path (``/login#claim=<token>``) and an expiry.

    Accepted oracle (deliberate, not an oversight): an unresolvable key answers 400 and a
    valid-but-not-yours key answers 403, so an authenticated caller can tell a live key
    from garbage. This adds NO capability the ``/api/auth/me`` carve-out does not already
    grant a caller holding a candidate key. The uniform-404 no-oracle rule governs the
    unauthenticated EXCHANGE surface, never this authed creation.
    """
    # OFF: access control disabled → refuse the claim-link mint with a named,
    # machine-readable reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    caller = await _pkg.resolve_caller()
    try:
        return await _create_claim_link(
            api_key=api_key,
            caller_id=caller.caller_id,
            caller_is_admin=caller.is_admin,
            caller_owner_claim=caller.owner_claim,
            ttl_seconds=ttl_seconds,
        )
    except ClaimLinkError as exc:
        # The store raises 400 (unresolvable key / bad ttl) or 403 (not the caller's key);
        # map each to the operation error the adapter renders at that status.
        if exc.status == 403:
            raise ForbiddenError(exc.message) from exc
        raise BadRequestError(exc.message) from exc
