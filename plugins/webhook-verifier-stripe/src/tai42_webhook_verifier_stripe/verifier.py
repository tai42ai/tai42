"""Stripe webhook-signature verifier.

Authenticates an inbound delivery's ``Stripe-Signature`` HMAC-SHA256. Stripe signs
the payload ``str(t).encode() + b"." + body`` — the timestamp, a literal ``.``, then
the exact raw body bytes — keyed by the endpoint's signing secret, and sends the
result as one or more ``v1=`` hex digests (multiple during a secret rotation). The
secret is named by ``config["secret_env"]`` and read from the environment at verify
time; a missing or empty secret raises loudly so the door fails CLOSED. Standard-library
only (``hmac`` /
``hashlib`` for the crypto, ``time`` for the freshness clock).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Mapping
from typing import Any

from tai42_contract.webhooks import WebhookVerificationError

_SIGNATURE_HEADER = "Stripe-Signature"
# A well-formed v1 digest is 64 hex chars (a SHA-256 digest rendered as hex).
_HEX_DIGEST_LEN = hashlib.sha256().digest_size * 2  # 64
# Stripe's documented default replay window, in seconds.
_DEFAULT_TOLERANCE_SECONDS = 300


class StripeWebhookVerifier:
    """Verifies a Stripe webhook delivery's ``Stripe-Signature`` HMAC.

    Registered under the name ``"stripe"`` on the app handle's ``webhook_verifiers``
    facet. Satisfies the :class:`~tai42_contract.webhooks.WebhookVerifier` protocol.
    """

    # Signature covers the raw body only, so a door binding this verifier must reject GET.
    post_only = True

    async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
        """Verify a Stripe webhook, or raise ``WebhookVerificationError``.

        ``body`` is the exact raw request bytes the HMAC is computed over; ``headers``
        are looked up case-insensitively; ``config`` is ``{"secret_env": "<ENV VAR
        NAME>"}`` with an optional ``"tolerance_seconds"`` (a positive int, default
        ``300``). Fails CLOSED: a missing config key, missing env var, empty secret, or
        non-positive/non-int tolerance raises loudly (``KeyError`` / ``ValueError``),
        never an ordinary signature failure.
        """
        # Fail closed: an empty secret would still key the HMAC into a signature anyone could forge.
        secret_env = config["secret_env"]
        secret = os.environ[secret_env].encode("utf-8")
        if not secret:
            raise ValueError(f"webhook secret environment variable {secret_env!r} is set but empty")

        tolerance_seconds = _tolerance_seconds(config)

        signature = _header_lookup(headers, _SIGNATURE_HEADER)
        if signature is None:
            raise WebhookVerificationError(f"missing {_SIGNATURE_HEADER} header")

        timestamp, candidates = _parse_signature_header(signature)

        # Stale-only replay defense: a delivery older than the tolerance window is
        # rejected. A future timestamp is NOT rejected — sender clock skew is not an
        # attack signal, and the delivery still carries a valid HMAC over this payload.
        if time.time() - timestamp > tolerance_seconds:
            raise WebhookVerificationError(f"{_SIGNATURE_HEADER} timestamp is older than the tolerance window")

        # str(timestamp) uses the parsed integer, so leading zeros or padding in the
        # header cannot sign a different payload than the freshness check ruled on. The
        # body stays raw bytes: the HMAC is defined over the transmitted bytes.
        signed_payload = str(timestamp).encode("ascii") + b"." + body
        expected_digest = hmac.new(secret, signed_payload, hashlib.sha256).digest()

        for candidate in candidates:
            candidate_digest = _decode_v1_candidate(candidate)
            # A malformed candidate (wrong length or non-hex) is dropped, not raised on:
            # a delivery mid-rotation carries several v1 signatures and one unusable
            # entry must not reject it. It also costs no comparison.
            if candidate_digest is None:
                continue
            # Constant-time compare so a mismatch position cannot be timed. Any well-formed
            # candidate matching passes verification (secret rotation sends several).
            if hmac.compare_digest(candidate_digest, expected_digest):
                return

        raise WebhookVerificationError(f"no {_SIGNATURE_HEADER} v1 signature matches the expected digest")


def _tolerance_seconds(config: dict[str, Any]) -> int:
    """Return the positive-int replay tolerance, defaulting to Stripe's ``300`` seconds.

    A present but non-int (``bool`` included, since it is not a sane duration) or
    non-positive value is an operator misconfiguration and raises ``ValueError`` — the
    door fails CLOSED rather than run with a nonsensical window.
    """
    tolerance = config.get("tolerance_seconds", _DEFAULT_TOLERANCE_SECONDS)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int):
        raise ValueError(f"tolerance_seconds must be an int, got {tolerance!r}")
    if tolerance <= 0:
        raise ValueError(f"tolerance_seconds must be positive, got {tolerance!r}")
    return tolerance


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    """Parse a ``Stripe-Signature`` value into its timestamp and its ``v1`` candidates.

    The value is a comma-separated list of ``key=value`` elements (split on the FIRST
    ``=``); surrounding whitespace on each element is tolerated. Exactly one ``t=``
    element is required and its value must be an integer; a missing, duplicate, or
    non-integer ``t`` is an unparsable header. Every ``v1=`` value is collected and at
    least one must be present. Any other scheme key (``v0=``, unrecognized) and any
    element without an ``=`` are ignored. Raises ``WebhookVerificationError`` on an
    unparsable header — the well-formedness of the digests themselves is checked later.
    """
    timestamps: list[str] = []
    candidates: list[str] = []
    for element in header.split(","):
        key, sep, value = element.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            candidates.append(value)

    if len(timestamps) != 1:
        raise WebhookVerificationError(f"{_SIGNATURE_HEADER} must carry exactly one t= element, got {len(timestamps)}")
    try:
        timestamp = int(timestamps[0])
    except ValueError as exc:
        raise WebhookVerificationError(f"{_SIGNATURE_HEADER} t= value is not an integer") from exc

    if not candidates:
        raise WebhookVerificationError(f"{_SIGNATURE_HEADER} carries no v1= signature")

    return timestamp, candidates


def _decode_v1_candidate(candidate: str) -> bytes | None:
    """Return a ``v1`` candidate's raw digest bytes, or ``None`` if it is malformed.

    A well-formed candidate is exactly 64 hex characters. A wrong length or a non-hex
    value is malformed and dropped (returns ``None``) rather than raised on.
    """
    if len(candidate) != _HEX_DIGEST_LEN:
        return None
    try:
        return bytes.fromhex(candidate)
    except ValueError:
        return None


def _header_lookup(headers: Mapping[str, str], name: str) -> str | None:
    """Return ``name``'s value from ``headers`` case-insensitively.

    HTTP header names are case-insensitive and the caller may pass a plain ``dict``.
    """
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


__all__ = ["StripeWebhookVerifier"]
