"""Webhook door authentication: the POST signature check and the GET handshake.

``POST`` bodies are signed with ``X-Hub-Signature-256`` = ``sha256=<hex>``
HMAC-SHA256 over the RAW body, validated fail-closed before the body is parsed;
``GET`` is Meta's subscription handshake, echoing ``hub.challenge`` only when
``hub.verify_token`` matches the configured token. An unset app secret / verify
token is a loud misconfiguration (logged 500), never a skipped check.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from tai42_kit.net.request_body import PayloadTooLarge, read_bounded_body
from tai42_kit.settings import require_secret

from tai42_channel_whatsapp.settings import whatsapp_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE_PREFIX = "sha256="
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class SignatureRejectedError(Exception):
    """The request failed X-Hub-Signature-256 authentication (mapped to 401)."""


def _validate_signature(app_secret: str, body: bytes, provided: str | None) -> None:
    """Validate ``X-Hub-Signature-256`` over the raw body or raise
    ``SignatureRejectedError``. Header form ``sha256=<hex>``; compared
    constant-time against the HMAC-SHA256 of the body under the app secret."""
    if provided is None:
        raise SignatureRejectedError(f"missing {_SIGNATURE_HEADER} header")
    if not provided.startswith(_SIGNATURE_PREFIX):
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not in sha256=<hex> form")
    try:
        # Decode to bytes first: a non-hex / non-ASCII header is a 401, never a
        # compare_digest TypeError (which would surface as a 500).
        provided_digest = bytes.fromhex(provided[len(_SIGNATURE_PREFIX) :])
    except ValueError as exc:
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not valid hex") from exc
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(provided_digest, expected):
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} mismatch")


async def _authenticated_body(request: Request) -> bytes:
    """Bounded-read and signature-validate the POST body; return the RAW bytes.
    Nothing in the body is trusted until the signature validates. Raises
    ``ValueError`` (app secret unset → logged 500), ``PayloadTooLarge``
    (→ 413), or ``SignatureRejectedError`` (→ 401)."""
    app_secret = require_secret(whatsapp_settings().app_secret, "WhatsApp channel", "CHANNEL_WHATSAPP_APP_SECRET")
    raw = await read_bounded_body(request, _MAX_BODY_BYTES)
    _validate_signature(app_secret, raw, request.headers.get(_SIGNATURE_HEADER))
    return raw


def _auth_error_response(exc: ValueError | PayloadTooLarge | SignatureRejectedError) -> Response:
    """Map an ``_authenticated_body`` failure to its response: 413 oversize, 401
    bad signature, 500 for an unset app secret (operator misconfig, never a 401
    that reads like an ordinary bad signature)."""
    if isinstance(exc, PayloadTooLarge):
        return PlainTextResponse("payload too large", status_code=413)
    if isinstance(exc, SignatureRejectedError):
        logger.warning("rejected whatsapp inbound: %s", exc)
        return PlainTextResponse("signature verification failed", status_code=401)
    logger.error("whatsapp inbound: CHANNEL_WHATSAPP_APP_SECRET is unset or empty; failing closed")
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


def _verify_handshake(request: Request) -> Response:
    """Meta's GET subscription handshake: echo ``hub.challenge`` iff
    ``hub.verify_token`` matches the configured token (constant-time), else 403.
    An unset verify token is a loud misconfiguration (logged 500)."""
    params = request.query_params
    if params.get("hub.mode") != "subscribe":
        return PlainTextResponse("unsupported hub.mode", status_code=403)
    try:
        expected = require_secret(whatsapp_settings().verify_token, "WhatsApp channel", "CHANNEL_WHATSAPP_VERIFY_TOKEN")
    except ValueError:
        logger.error("whatsapp verify: CHANNEL_WHATSAPP_VERIFY_TOKEN is unset or empty; failing closed")
        return JSONResponse({"error": "channel misconfigured"}, status_code=500)
    provided = params.get("hub.verify_token")
    # A non-ASCII token can never match the configured token and would raise a
    # compare_digest TypeError (a 500); treat it as a mismatch (403).
    if provided is None or not provided.isascii() or not hmac.compare_digest(provided, expected):
        logger.warning("rejected whatsapp verify: hub.verify_token mismatch")
        return PlainTextResponse("verification failed", status_code=403)
    challenge = params.get("hub.challenge")
    if challenge is None:
        return PlainTextResponse("missing hub.challenge", status_code=400)
    return PlainTextResponse(challenge)
