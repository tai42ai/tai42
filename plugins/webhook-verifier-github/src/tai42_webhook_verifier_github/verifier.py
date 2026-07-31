"""GitHub webhook-signature verifier.

Authenticates an inbound delivery's ``X-Hub-Signature-256`` HMAC-SHA256 (over the
exact raw body, keyed by the shared secret). The secret is named by
``config["secret_env"]`` and read from the environment at verify time; a missing or
empty secret raises loudly so the door fails CLOSED. Standard-library only
(``hmac`` / ``hashlib``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import Any

from tai42_contract.webhooks import WebhookVerificationError

# A well-formed value is ``sha256=`` followed by 64 hex chars.
_SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE_PREFIX = "sha256="
_HEX_DIGEST_LEN = hashlib.sha256().digest_size * 2  # 64


class GitHubWebhookVerifier:
    """Verifies a GitHub webhook delivery's ``X-Hub-Signature-256`` HMAC.

    Registered under the name ``"github"`` on the app handle's
    ``webhook_verifiers`` facet. Satisfies the
    :class:`~tai42_contract.webhooks.WebhookVerifier` protocol.
    """

    # Signature covers the raw body only, so a door binding this verifier must reject GET.
    post_only = True

    async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
        """Verify a GitHub webhook, or raise ``WebhookVerificationError``.

        ``body`` is the exact raw request bytes the HMAC is computed over; ``headers``
        are looked up case-insensitively; ``config`` is
        ``{"secret_env": "<ENV VAR NAME>"}``. Fails CLOSED: a missing config key,
        missing env var, or empty secret raises loudly (``KeyError`` / ``ValueError``),
        never an ordinary signature failure.
        """
        # Fail closed: an empty secret would still key the HMAC into a signature anyone could forge.
        secret_env = config["secret_env"]
        secret = os.environ[secret_env].encode("utf-8")
        if not secret:
            raise ValueError(f"webhook secret environment variable {secret_env!r} is set but empty")

        signature = _header_lookup(headers, _SIGNATURE_HEADER)
        if signature is None:
            raise WebhookVerificationError(f"missing {_SIGNATURE_HEADER} header")

        if not signature.startswith(_SIGNATURE_PREFIX):
            raise WebhookVerificationError(f"{_SIGNATURE_HEADER} is not prefixed {_SIGNATURE_PREFIX!r}")

        provided_hex = signature[len(_SIGNATURE_PREFIX) :]
        if len(provided_hex) != _HEX_DIGEST_LEN:
            raise WebhookVerificationError(f"{_SIGNATURE_HEADER} digest is not {_HEX_DIGEST_LEN} hex characters")
        # Reject a non-hex digest as an ordinary verification failure, not an unhandled parse error.
        try:
            provided_digest = bytes.fromhex(provided_hex)
        except ValueError as exc:
            raise WebhookVerificationError(f"{_SIGNATURE_HEADER} digest is not valid hex") from exc

        expected_digest = hmac.new(secret, body, hashlib.sha256).digest()

        # Constant-time compare so a mismatch position cannot be timed.
        if not hmac.compare_digest(provided_digest, expected_digest):
            raise WebhookVerificationError(f"{_SIGNATURE_HEADER} digest mismatch")


def _header_lookup(headers: Mapping[str, str], name: str) -> str | None:
    """Return ``name``'s value from ``headers`` case-insensitively.

    HTTP header names are case-insensitive and the caller may pass a plain ``dict``.
    """
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


__all__ = ["GitHubWebhookVerifier"]
