"""The entry-gate management doors (authed, platform api key): read/toggle the gate, mint/revoke entry codes.

Unlike the public chat doors these require the platform api key (declared ``public:
false``) and declare an explicit ``read``/``write`` action-class — an authed route
with none REFUSES TO REGISTER (fail-closed fence). They refuse with the standard JSON
envelope; they are not navigations and get no refusal pages.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.app import tai42_app

from tai42_channel_web.routes.dtos import (
    _IDENTITY_REQUIREMENT,
    CodeRevokedResponse,
    GateStateResponse,
    GateToggleBody,
    GateToggledResponse,
    MintCodeBody,
    MintedCodeResponse,
    _clean_identity,
)
from tai42_channel_web.routes.envelope import _body_refusal, _error, _json_body, _ok
from tai42_channel_web.routes.session_access import _store_off
from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.entry_gate import (
    EntryCode,
    is_gate_enabled,
    list_entry_codes,
    mint_entry_code,
    revoke_entry_code,
    set_gate,
)


def _managed_identity(request: Request) -> str | None:
    """The canonical identity a management door acts on, or ``None`` when the path segment is unusable.

    The door answers a 422 for the ``None`` case.
    """
    return _clean_identity(request.path_params["identity"])


def _code_view(code: EntryCode) -> dict[str, Any]:
    return {
        "code_id": code.code_id,
        "label": code.label,
        "created_at": code.created_at,
        "expires_at": code.expires_at,
    }


@tai42_app.http.custom_route(
    "/gates/{identity}",
    methods=["GET"],
    summary="Read a web route's entry-gate state and its codes",
    tags=["channels"],
    response_model=GateStateResponse,
    action="read",
)
async def web_gate_read(request: Request) -> Response:
    """The gate flag for a web route and the live codes minted for it.

    Never the raw codes — only their ids and metadata.
    """
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    enabled = await is_gate_enabled(identity)
    codes = await list_entry_codes(identity)
    return _ok({"enabled": enabled, "codes": [_code_view(code) for code in codes]})


@tai42_app.http.custom_route(
    "/gates/{identity}",
    methods=["PUT"],
    summary="Turn a web route's entry gate on or off",
    tags=["channels"],
    response_model=GateToggledResponse,
    request_model=GateToggleBody,
    action="write",
)
async def web_gate_toggle(request: Request) -> Response:
    """Set the explicit gate flag.

    Turning it off does not touch the codes; turning it on with no live code makes the
    route unreachable until one is minted.
    """
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = GateToggleBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    await set_gate(identity, body.enabled)
    return _ok({"enabled": body.enabled})


@tai42_app.http.custom_route(
    "/gates/{identity}/codes",
    methods=["POST"],
    summary="Mint an entry code for a web route",
    tags=["channels"],
    response_model=MintedCodeResponse,
    request_model=MintCodeBody,
    action="write",
)
async def web_gate_mint_code(request: Request) -> Response:
    """Mint a multi-use entry code.

    The raw code is returned ONCE, here — only its hash is stored, so it can never be
    read back.
    """
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = MintCodeBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    raw_code, code_id = await mint_entry_code(identity, body.label, body.expires_at)
    expires_at = body.expires_at.astimezone(UTC).isoformat() if body.expires_at is not None else None
    return _ok({"code": raw_code, "code_id": code_id, "expires_at": expires_at})


@tai42_app.http.custom_route(
    "/gates/{identity}/codes/{code_id}",
    methods=["DELETE"],
    summary="Revoke a web route's entry code",
    tags=["channels"],
    response_model=CodeRevokedResponse,
    action="write",
)
async def web_gate_revoke_code(request: Request) -> Response:
    """Revoke one code by its id; an unknown id is a 404 envelope error."""
    identity = _managed_identity(request)
    if identity is None:
        return _error(_IDENTITY_REQUIREMENT, 422)
    off = _store_off()
    if off is not None:
        return off
    code_id = request.path_params["code_id"]
    if not await revoke_entry_code(identity, code_id):
        return _error("entry code not found", 404)
    return _ok({"status": "revoked"})
