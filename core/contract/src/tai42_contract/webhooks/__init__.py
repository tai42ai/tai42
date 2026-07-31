"""Webhook signature verification contracts.

A :class:`WebhookVerifier` authenticates an inbound webhook from its raw request
bytes and headers, BEFORE the platform parses or dispatches the payload.
Verifiers are registered on the app handle (``tai42_app.webhook_verifiers``) by
provider plugins and looked up by name when a public webhook door has a verifier
bound to it. Verification either returns ``None`` (success) or raises
:class:`WebhookVerificationError` (any failure) — never a bool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


class WebhookVerificationError(Exception):
    """Raised by a :class:`WebhookVerifier` when an inbound webhook fails
    verification.

    Every failure mode — a missing or malformed signature header, a wrong-length
    or mis-prefixed signature, a digest mismatch, a missing secret — raises this
    single typed error. A verifier NEVER returns a bool: a forgotten check must
    not read as a silent pass, so the only success signal is a plain return.
    """


@runtime_checkable
class WebhookVerifier(Protocol):
    """Authenticates an inbound webhook over its raw bytes + headers.

    Optional attribute ``post_only`` (a bool, read by doors via ``getattr`` with
    a default of ``False``): a body-signature verifier — one that authenticates
    the raw request body — sets ``post_only = True`` so a door that also accepts
    GET rejects GET delivery for a topic bound to it (a GET door would sign an
    empty body while the real payload rides the URL unauthenticated). A
    header-based verifier leaves it unset/``False`` and works over any method.
    """

    async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
        """Verify an inbound webhook, or raise :class:`WebhookVerificationError`.

        ``body`` is the EXACT raw request bytes. Signature schemes compute their
        HMAC over these bytes, so a door MUST call ``verify`` with the raw body
        BEFORE any parsing and parse only after this returns.

        ``headers`` are the request headers (case-insensitive lookup expected).

        ``config`` is the per-binding configuration. It NEVER holds a secret
        value — only a ``secret_env`` naming the environment variable that holds
        the secret, resolved here at verify time. A missing env var raises loudly
        (verification fails CLOSED, never soft-open).

        A body-signature verifier authenticates the raw body only, so it binds to
        POST delivery exclusively: a door that also accepts GET must reject GET
        for a body-signature-bound topic, or the real payload would ride the URL
        unauthenticated. Returns ``None`` on success.
        """
        ...


__all__ = ["WebhookVerificationError", "WebhookVerifier"]
