"""The HTTP envelope both ways: build a success/refusal response and read a bounded JSON body."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body

from tai42_channel_web.page import HTML_CONTENT_TYPE, REFUSAL_CSP
from tai42_channel_web.settings import WebSettings

logger = logging.getLogger(__name__)

# Everything these doors answer is about one caller at one moment: the page carries a
# fresh session cookie and links the current build's hashes, and a refusal describes
# this caller's session or origin. A cached copy would hand a later visitor any of it
# — and 404 and 501 are cacheable by default without this.
_NO_STORE = "no-store"

# Every response's declared type must be honored, never MIME-sniffed into something
# executable — the page's CSP script gate assumes it, and the asset door serves
# visitor-reachable bytes.
_NOSNIFF = {"x-content-type-options": "nosniff"}

# A capability URL must never leak via the ``Referer`` header — set on EVERY
# page-door HTML response (the chat page and every refusal page).
_REFERRER_POLICY = {"referrer-policy": "no-referrer"}


def _error(message: str, status_code: int, code: str | None = None) -> JSONResponse:
    """A refusal as the API doors answer it.

    Never cached: a refusal is about this caller at this moment, and several of these statuses
    (404, 501) are heuristically cacheable — a cached one would answer a later visitor on a GET
    door.
    """
    payload: dict[str, str] = {"error": message}
    if code is not None:
        payload["code"] = code
    headers = {**_NOSNIFF, "cache-control": _NO_STORE}
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _refusal_page(html: str, status_code: int) -> Response:
    """A refusal the PAGE door answers.

    The caller is a browser navigating to the chat URL, so it is served one of the byte-constant
    pages above rather than the API doors' JSON. Cached as little as the page itself — a refusal
    is about this caller at this moment.
    """
    headers = {"content-security-policy": REFUSAL_CSP, **_NOSNIFF, "cache-control": _NO_STORE, **_REFERRER_POLICY}
    return Response(html, status_code=status_code, media_type=HTML_CONTENT_TYPE, headers=headers)


def _ok(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"data": data}, headers=_NOSNIFF)


async def _json_body(request: Request, settings: WebSettings) -> tuple[Any, JSONResponse | None]:
    """The parsed JSON body, or ``(None, <refusal>)`` for an over-cap or unparseable one."""
    try:
        raw = await read_bounded_body(request, settings.max_body_bytes)
    except RequestBodyTooLargeError as exc:
        logger.warning("web chat door refused an oversized body: %s", exc)
        return None, _error("request body is too large", 413)
    try:
        # ``RecursionError`` is what ``json.loads`` raises past the interpreter's
        # recursion limit, and a body far under the byte cap nests deep enough to
        # reach it — the same clean refusal the contract's iterative depth walk
        # gives deep nesting, never an opaque 500 from a public door.
        return json.loads(raw), None
    except (ValueError, RecursionError):
        return None, _error("invalid JSON body", 400)


def _body_refusal(exc: ValidationError) -> str:
    """The first field-level reason a message body was refused.

    So a caller can tell a malformed retry key from an unusable identity or an over-long text.
    Every part quoted is this door's own field name or message.
    """
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "body"
    return f"invalid request body: {field}: {first['msg']}"
