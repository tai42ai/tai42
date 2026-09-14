"""Slack transport authentication shared by both inbound doors.

The v0 HMAC signature check over the exact raw body, and the bounded, verified
body read that fronts every door: a loud 413 past the body cap (before any HMAC
work), a logged 500 on a missing signing secret (fail closed), a uniform 401 on
any signature defect.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_kit.net.request_body import PayloadTooLarge, read_bounded_body

from tai42_channel_slack.settings import slack_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Slack-Signature"
_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
_SIGNATURE_PREFIX = "v0="
_HEX_DIGEST_LEN = hashlib.sha256().digest_size * 2  # 64
# Slack's replay window: reject a timestamp more than five minutes from now.
_MAX_TIMESTAMP_SKEW_SECONDS = 300
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class _SignatureRejected(Exception):
    """An ordinary request-side verification failure -> uniform 401."""


class _InboundRejected(Exception):
    """A transport-auth failure carrying the vendor-facing response a door returns
    instead of proceeding: 413 (too large), 500 (misconfigured), or 401 (rejected)."""

    def __init__(self, response: Response) -> None:
        super().__init__()
        self.response = response


def _misconfigured(env_name: str) -> JSONResponse:
    """Fail CLOSED on operator misconfiguration: one logged, constant JSON 500."""
    logger.error("slack inbound: %s is unset or empty; failing closed", env_name)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


def _verify_signature(raw: bytes, headers: Mapping[str, str], secret: str) -> None:
    """Authenticate ``raw`` against Slack's v0 signing scheme, or raise.

    Every request-side defect raises :class:`_SignatureRejected`, mapped to one
    constant 401 — no oracle distinguishing the failure reason.
    """
    timestamp = headers.get(_TIMESTAMP_HEADER)
    if timestamp is None:
        raise _SignatureRejected(f"missing {_TIMESTAMP_HEADER} header")
    # Gate to ASCII digits before int(): bare int() also accepts unicode whitespace
    # and non-ASCII digits, which must be the same uniform reject.
    if not (timestamp.isascii() and timestamp.isdigit()):
        raise _SignatureRejected(f"{_TIMESTAMP_HEADER} is not an ASCII-digit integer")
    ts_value = int(timestamp)
    if abs(time.time() - ts_value) > _MAX_TIMESTAMP_SKEW_SECONDS:
        raise _SignatureRejected("request timestamp outside the replay window")

    signature = headers.get(_SIGNATURE_HEADER)
    if signature is None:
        raise _SignatureRejected(f"missing {_SIGNATURE_HEADER} header")
    if not signature.startswith(_SIGNATURE_PREFIX):
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} is not prefixed {_SIGNATURE_PREFIX!r}")
    provided_hex = signature[len(_SIGNATURE_PREFIX) :]
    if len(provided_hex) != _HEX_DIGEST_LEN:
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest is not {_HEX_DIGEST_LEN} hex characters")
    try:
        provided_digest = bytes.fromhex(provided_hex)
    except ValueError as exc:
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest is not valid hex") from exc

    base = b"v0:" + timestamp.encode("ascii") + b":" + raw
    expected_digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).digest()
    # Constant-time compare so a mismatch position cannot be timed.
    if not hmac.compare_digest(provided_digest, expected_digest):
        raise _SignatureRejected(f"{_SIGNATURE_HEADER} digest mismatch")


async def _read_verified_body(request: Request) -> bytes:
    """Read the bounded raw body and authenticate it, returning the exact bytes.

    Bounds the read BEFORE any HMAC work and resolves the signing secret first (an
    unset or empty key would make the HMAC forgeable). Raises :class:`_InboundRejected`
    carrying the response a door returns without proceeding: 413 past the cap, 500 on a
    missing secret, 401 on any signature defect (the reason stays in the log).
    """
    try:
        raw = await read_bounded_body(request, _MAX_BODY_BYTES)
    except PayloadTooLarge as exc:
        raise _InboundRejected(JSONResponse({"error": "payload too large"}, status_code=413)) from exc
    signing_secret = slack_settings().signing_secret
    secret = signing_secret.get_secret_value() if signing_secret is not None else ""
    if not secret:
        raise _InboundRejected(_misconfigured("CHANNEL_SLACK_SIGNING_SECRET"))
    try:
        _verify_signature(raw, request.headers, secret)
    except _SignatureRejected as exc:
        logger.warning("slack inbound door rejected: %s", exc)
        raise _InboundRejected(JSONResponse({"error": "signature verification failed"}, status_code=401)) from exc
    return raw
