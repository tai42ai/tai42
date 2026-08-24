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
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from tai42_contract.errors import ErrorKind


class WebhookVerificationError(Exception):
    """Raised by a :class:`WebhookVerifier` when an inbound webhook fails
    verification.

    Every failure mode — a missing or malformed signature header, a wrong-length
    or mis-prefixed signature, a digest mismatch, a missing secret — raises this
    single typed error. A verifier NEVER returns a bool: a forgotten check must
    not read as a silent pass, so the only success signal is a plain return.
    """

    # A failed signature/secret verification is an authentication failure.
    __tai_error_kind__ = ErrorKind.UNAUTHORIZED


class ReplayDefense:
    """A verifier's declaration of how the delivery it just authenticated is
    defended against replay. ABSTRACT: :meth:`WebhookVerifier.replay_defense`
    returns one of the two concrete forms below (:class:`SeenSetClaim` or
    :class:`FreshnessWindow`); the base itself cannot be instantiated, so a
    scheme cannot hand back a bare ReplayDefense that names no defense yet
    slips past an ``isinstance(..., ReplayDefense)`` gate as if it declared one.
    """

    def __init__(self) -> None:
        # Abstract: only a concrete subclass carries a real defense declaration. A
        # bare instance would satisfy the ingress ``isinstance`` gate while defending
        # nothing, so it is refused at construction — the door then fails CLOSED.
        raise TypeError("ReplayDefense is abstract; return a SeenSetClaim or a FreshnessWindow")


@dataclass(frozen=True)
class SeenSetClaim(ReplayDefense):
    """Defend replay by a seen-set: the ingress atomically claims ``key`` and
    refuses a second delivery that presents the same ``key`` within
    ``ttl_seconds``. ``key`` is a STABLE per-delivery id the sender supplies (a
    delivery UUID, a nonce) — never a body hash, so two distinct deliveries that
    happen to carry identical bodies are not conflated. A scheme with no freshness
    of its own relies on the seen-set as its whole replay defense; a scheme that
    DOES sign a timestamp may still return this form to add within-window dedup on
    top of its own stale-rejection. Either way the seen-set is the replay defense
    here and ``ttl_seconds`` is the bounded window over which it holds."""

    key: str
    ttl_seconds: int

    def __post_init__(self) -> None:
        # A claim without a positive TTL would leak a permanent key (or, at zero,
        # defend nothing); an empty key would collapse every delivery to one slot.
        if not self.key:
            raise ValueError("SeenSetClaim requires a non-empty key")
        # Exact-type check: excludes ``bool`` (an int subclass that is not a sane
        # duration) and any non-int the annotation was bypassed with at runtime.
        if type(self.ttl_seconds) is not int:
            raise ValueError(f"SeenSetClaim ttl_seconds must be an int, got {self.ttl_seconds!r}")
        if self.ttl_seconds <= 0:
            raise ValueError(f"SeenSetClaim ttl_seconds must be positive, got {self.ttl_seconds!r}")


@dataclass(frozen=True)
class FreshnessWindow(ReplayDefense):
    """Defend replay by the verifier's OWN signed freshness window: ``verify``
    already rejected a delivery whose signed timestamp is outside the tolerance,
    so a captured delivery replays only until it goes stale and the ingress needs
    no seen-set. Returned by a scheme that signs a timestamp."""


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

    def replay_defense(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> ReplayDefense:
        """Declare how the delivery :meth:`verify` just authenticated is defended
        against replay — a per-scheme property, so no verifier can ship without one.

        Called by a fan-out door ONLY after :meth:`verify` returns success, over the
        SAME raw ``body``, ``headers`` and per-binding ``config``. Returns either a
        :class:`SeenSetClaim` (the ingress dedups a stable per-delivery id it carries)
        or a :class:`FreshnessWindow` (``verify`` already stale-rejects via a signed
        timestamp, so no seen-set is needed).

        Fails CLOSED: a scheme that needs a delivery-id/nonce header to be replay-safe
        raises :class:`WebhookVerificationError` when that header is absent, so the door
        refuses rather than dispatching undefended. A binding misconfiguration (a
        required config key absent) raises a plain exception, exactly as ``verify`` does.
        Pure (no IO): a header + config read only.
        """
        ...


__all__ = [
    "FreshnessWindow",
    "ReplayDefense",
    "SeenSetClaim",
    "WebhookVerificationError",
    "WebhookVerifier",
]
