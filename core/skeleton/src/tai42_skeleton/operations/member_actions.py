"""The member-actions operations: declare the catalog, invoke an action by key.

An accounts provider declares its own member-admin actions through the contract
(:meth:`~tai42_contract.accounts.provider.AccountsProvider.member_actions`); this module
exposes two generic operations over every registered provider — ``list_member_actions``
builds the deployment-wide catalog and ``invoke_member_action`` routes a single call back
to the provider that owns the action. The platform never branches on what an action means:
it renders the label and the input/result JSON schemas generically and validates the
per-action input with ordinary pydantic.

The routing tokens are opaque. A per-row HANDLE encodes ``(provider, target_id)`` and a
per-action KEY encodes ``(provider, action_id)``; both are base64-of-JSON the platform mints
and decodes to route a call to its own registered provider, never a field a caller parses.
They carry the platform's OWN registry provider name — runtime routing data to the
platform's registered providers, not provider business vocabulary, and never shipped data or
a generated schema.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from pydantic import ValidationError
from tai42_contract.accounts import (
    AccountsProvider,
    InvokeMemberActionRequest,
    InvokeMemberActionResult,
    MemberAction,
    MemberActionCatalog,
    MemberActionDescriptor,
)
from tai42_contract.accounts.errors import (
    MemberActionBadRequestError,
    MemberActionConflictError,
    MemberActionError,
    MemberActionNotFoundError,
)
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    OperationError,
    ValidationRejectedError,
    operation,
)
from tai42_skeleton.operations._authority import require_admin, resolve_caller
from tai42_skeleton.operations.adapter import validation_error_fields

_HANDLE_PROVIDER_KEY = "p"
_HANDLE_TARGET_KEY = "t"
_ACTION_PROVIDER_KEY = "p"
_ACTION_ID_KEY = "a"


def _encode_token(payload: dict[str, str]) -> str:
    """Encode a compact tagged mapping as one urlsafe base64 opaque token."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_token(token: str) -> dict[str, Any]:
    """Decode an opaque token back to its mapping, or raise a loud :class:`BadRequestError`.

    A token that is not urlsafe base64 of a JSON object is malformed — refused, never a
    silent default.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise BadRequestError("malformed member-action token") from exc
    if not isinstance(decoded, dict):
        raise BadRequestError("malformed member-action token")
    return decoded


def _encode_handle(provider: str, target: str) -> str:
    """The opaque routing handle for a row: ``encode(provider, target_id)``."""
    return _encode_token({_HANDLE_PROVIDER_KEY: provider, _HANDLE_TARGET_KEY: target})


def _decode_handle(token: str) -> tuple[str, str]:
    """``(provider, target)`` from a routing handle, or raise :class:`BadRequestError`."""
    decoded = _decode_token(token)
    provider = decoded.get(_HANDLE_PROVIDER_KEY)
    target = decoded.get(_HANDLE_TARGET_KEY)
    if not isinstance(provider, str) or not provider or not isinstance(target, str) or not target:
        raise BadRequestError("malformed member-action target handle")
    return provider, target


def _encode_action_key(provider: str, action_id: str) -> str:
    """The opaque catalog key for an action: ``encode(provider, action_id)``."""
    return _encode_token({_ACTION_PROVIDER_KEY: provider, _ACTION_ID_KEY: action_id})


def _decode_action_key(token: str) -> tuple[str, str]:
    """``(provider, action_id)`` from a catalog key, or raise :class:`BadRequestError`."""
    decoded = _decode_token(token)
    provider = decoded.get(_ACTION_PROVIDER_KEY)
    action_id = decoded.get(_ACTION_ID_KEY)
    if not isinstance(provider, str) or not provider or not isinstance(action_id, str) or not action_id:
        raise BadRequestError("malformed member-action key")
    return provider, action_id


def _active_accounts_provider_items() -> list[tuple[str, AccountsProvider]]:
    """The CURRENT epoch's live accounts providers as name-sorted ``(name, provider)`` pairs.

    The registry name is the platform routing key the opaque tokens embed, so the Members
    surface enumerates providers WITH their names. Reads the serving core so a build in
    flight resolves the epoch being built, else the live one — the same source
    ``list_members`` and ``login`` read.
    """
    from tai42_skeleton.app.instance import app

    recorded = app._serving_core.active_auth_providers
    return [(name, provider) for name, provider in sorted(recorded.items()) if isinstance(provider, AccountsProvider)]


def _resolve_active_accounts_provider(name: str) -> AccountsProvider:
    """The live accounts provider registered under ``name``, or raise :class:`NotFoundError`."""
    for provider_name, provider in _active_accounts_provider_items():
        if provider_name == name:
            return provider
    raise NotFoundError(f"no active accounts provider {name!r}")


@operation(
    summary="List member actions",
    tags=["access-control"],
    errors=[ForbiddenError],
    response_model=MemberActionCatalog,
)
async def list_member_actions() -> MemberActionCatalog:
    """The deployment-wide catalog of declared member actions (admin only).

    Each registered accounts provider's :meth:`member_actions` declarations are serialized
    into opaque-keyed :class:`MemberActionDescriptor` rows, the label resolved through the
    resource manager and the input/result schemas rendered generically. The platform reads
    no action field.
    """
    caller = await resolve_caller()
    require_admin(caller)
    resource_manager = tai42_app.storage.resource_manager
    descriptors: list[MemberActionDescriptor] = []
    for name, provider in _active_accounts_provider_items():
        for action in provider.member_actions():
            label = await resource_manager.render_templated_text(action.label)
            descriptors.append(
                MemberActionDescriptor(
                    key=_encode_action_key(name, action.id),
                    label=label,
                    scope=action.scope,
                    destructive=action.destructive,
                    input_schema=action.input_model.model_json_schema(),
                    result_schema=action.result_model.model_json_schema(),
                )
            )
    return MemberActionCatalog(actions=descriptors)


@operation(
    summary="Invoke a member action",
    tags=["access-control"],
    authority_changing=True,
    errors=[ForbiddenError, BadRequestError, NotFoundError, ValidationRejectedError, ConflictError],
    request_model=InvokeMemberActionRequest,
    response_model=InvokeMemberActionResult,
)
async def invoke_member_action(
    action_key: str,
    target_handle: str | None,
    input: dict[str, Any],  # noqa: A002 the parameter mirrors the InvokeMemberActionRequest wire field name
) -> InvokeMemberActionResult:
    """Invoke one declared member action by its opaque key (admin only).

    The key decodes to ``(provider, action_id)`` and the optional handle to the
    ``(provider, target)`` the action acts on; a malformed token or a key/handle naming
    different providers is a loud :class:`BadRequestError`. The per-action ``input`` is
    validated by ``input_model.model_validate`` (ordinary pydantic — a declared-field type
    error is a 422, an unknown key is ignored). The provider's typed failure propagates and
    the platform reads no field of the opaque result.
    """
    caller = await resolve_caller()
    require_admin(caller)
    provider_name, action_id = _decode_action_key(action_key)
    target: str | None = None
    if target_handle is not None:
        handle_provider, target = _decode_handle(target_handle)
        if handle_provider != provider_name:
            raise BadRequestError("action key and target handle name different providers")
    provider = _resolve_active_accounts_provider(provider_name)
    action = _find_member_action(provider, action_id)
    try:
        payload = action.input_model.model_validate(input)
    except ValidationError as exc:
        raise ValidationRejectedError("invalid action input", extra={"error": validation_error_fields(exc)}) from exc
    try:
        result = await provider.invoke_member_action(action_id, target=target, payload=payload)
    except MemberActionError as exc:
        raise _mapped_member_action_error(exc) from exc
    return InvokeMemberActionResult(result=result.model_dump(mode="json"))


def _mapped_member_action_error(exc: MemberActionError) -> OperationError:
    """Map a provider's contract member-action error to the operations-layer error for its status.

    A contract-only accounts provider cannot import the operations errors, so it raises the
    generic :class:`MemberActionError` family; the operation maps each to the matching typed
    operation error (409 conflict, 404 not-found, 400 bad request, else 422 rejected input)
    so a correctable provider failure reaches the right status instead of a generic 500. The
    message is surfaced unchanged; no provider-specific content is read.
    """
    if isinstance(exc, MemberActionConflictError):
        return ConflictError(str(exc))
    if isinstance(exc, MemberActionNotFoundError):
        return NotFoundError(str(exc))
    if isinstance(exc, MemberActionBadRequestError):
        return BadRequestError(str(exc))
    return ValidationRejectedError(str(exc))


def _find_member_action(provider: AccountsProvider, action_id: str) -> MemberAction:
    """The provider's declared action named ``action_id``, or raise :class:`NotFoundError`."""
    for action in provider.member_actions():
        if action.id == action_id:
            return action
    raise NotFoundError(f"no member action {action_id!r}")
