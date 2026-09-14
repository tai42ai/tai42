"""The session-rotation door: mint a fresh session for one web route."""

from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.app import tai42_app

from tai42_channel_web.routes.dtos import RotateBody, SessionRotatedResponse
from tai42_channel_web.routes.entry_admission import _ENTRY_REFUSED_CODE, _ENTRY_REFUSED_MESSAGE, _entry_gate_outcome
from tai42_channel_web.routes.envelope import _body_refusal, _error, _json_body, _ok
from tai42_channel_web.routes.session_access import (
    _CROSS_ORIGIN,
    _CROSS_ORIGIN_CODE,
    _mint_session,
    _mount_base,
    _store_off,
)
from tai42_channel_web.session import is_cross_origin, session_token
from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.registrations import drop_session


@tai42_app.http.custom_route(
    "/session/rotate",
    methods=["POST"],
    summary="Start a fresh web chat visitor session",
    tags=["channels"],
    response_model=SessionRotatedResponse,
)
async def web_session_rotate(request: Request) -> Response:
    """Mint a fresh session for one web route and set it as the visitor's cookie.

    The body names the route, because a session is bound to one and this door mints
    without reading a URL: the fresh session serves that route and no other. It is not
    a credential check — anyone may open the chat page of any route and be minted a
    session there — so no session is required to rotate, exactly as before.

    The old registration is DELETED, so the token that reached this door can never
    address the old conversation again. The new visitor id holds no transcript, so
    the conversation starts empty on the next message; the old transcript is
    untouched and ages out on its own TTL.
    """
    if is_cross_origin(request):
        return _error(_CROSS_ORIGIN, 403, _CROSS_ORIGIN_CODE)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    raw, refusal = await _json_body(request, settings)
    if refusal is not None:
        return refusal
    try:
        body = RotateBody.model_validate(raw)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)
    # A rotation mints a session, so a gated identity gates it too — same throttle then
    # code check as the page refusal. An ungated identity ignores the code.
    if await _entry_gate_outcome(request, body.identity, body.entry_code) is not None:
        return _error(_ENTRY_REFUSED_MESSAGE, 403, _ENTRY_REFUSED_CODE)
    token = session_token(request, settings)
    if token is not None:
        await drop_session(token)
    response = _ok({"status": "rotated"})
    # A rotation mints a CLEAN registration: it carries no link params (README pin).
    await _mint_session(response, body.identity, settings, {}, _mount_base(request, "/session/rotate"))
    return response
