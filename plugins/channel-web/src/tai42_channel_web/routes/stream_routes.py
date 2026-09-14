"""The SSE stream door: open the visitor's own conversation feed (backlog then tail)."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from tai42_contract.app import tai42_app

from tai42_channel_web.routes.dtos import _MAX_IDENTITY_CHARS, _clean_identity
from tai42_channel_web.routes.envelope import _NOSNIFF, _error
from tai42_channel_web.routes.session_access import (
    _SESSION_MISSING,
    _SESSION_MISSING_CODE,
    _serves,
    _session,
    _store_off,
)
from tai42_channel_web.settings import web_settings
from tai42_channel_web.stream import StreamLimitError, check_stream_admission, stream_transcript

logger = logging.getLogger(__name__)


@tai42_app.http.custom_route(
    "/stream",
    methods=["GET"],
    summary="Stream the visitor's web conversation (backlog then live)",
    tags=["channels"],
    response_model=None,
    no_body_reason="SSE StreamingResponse: text/event-stream, no fixed body",
)
async def web_stream(request: Request) -> Response:
    """Open the SSE feed of the session's own conversation.

    The transcript is keyed by ``(identity, visitor id)``, so a visitor only ever
    sees the conversation their own session addresses — and only on the web route
    their session was minted on, refused as a missing session otherwise. An
    unconfigured transcript store answers a plain 501+code up front rather than
    sending 200 + SSE headers and then dying mid-body, and an over-cap caller a plain
    503 that names which ceiling it hit — each open stream pins a dedicated Redis
    connection for its whole life.

    The caps are checked here but TAKEN by the generator: see
    ``check_stream_admission``.
    """
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    registration = await _session(request, settings)
    if registration is None:
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    requested = request.query_params.get("identity")
    if requested is None:
        return _error("the 'identity' query parameter is required", 400)
    identity = _clean_identity(requested)
    if identity is None:
        return _error(
            "the 'identity' query parameter must be a non-blank, ':'-free web route identity "
            f"of at most {_MAX_IDENTITY_CHARS} characters",
            400,
        )
    if not _serves(registration, identity):
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    address = registration.visitor_id
    try:
        check_stream_admission(address, settings)
    except StreamLimitError as exc:
        logger.warning("web chat stream refused: %s", exc)
        return _error(exc.visitor_message, 503)
    return StreamingResponse(
        stream_transcript(request, identity, address, settings),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", **_NOSNIFF},
    )
