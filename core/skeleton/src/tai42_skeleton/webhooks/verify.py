"""The one webhook verification both verifying doors run over a request's raw bytes.

``verify_webhook`` resolves the binding's named verifier, refuses a GET on a body-signature
verifier, runs the verifier, and (when the door asks) computes its replay declaration over the same
bytes. Every failure raises :class:`WebhookVerifyFailure` carrying the door's status and its
constant public message, so each door answers the same bodies through its own JSON helper. The
public message never says which check failed; the reason is logged, the raw body never is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind
from tai42_contract.hooks import TopicVerifierBinding
from tai42_contract.webhooks import ReplayDefense, WebhookVerificationError

logger = logging.getLogger(__name__)

__all__ = [
    "POST_ONLY",
    "VERIFY_ERROR",
    "VERIFY_FAILED",
    "WebhookVerdict",
    "WebhookVerifyFailure",
    "verify_webhook",
]

VERIFY_FAILED: Final = "webhook verification failed"
VERIFY_ERROR: Final = "webhook verification error"
POST_ONLY: Final = "this verified topic accepts POST delivery only"

_KIND_BY_STATUS: Final = {401: ErrorKind.UNAUTHORIZED, 405: ErrorKind.BAD_INPUT, 500: ErrorKind.UNKNOWN}


@dataclass(frozen=True)
class WebhookVerdict:
    """A passing verification: whether the verifier signs the body only, and its replay declaration if asked."""

    post_only: bool
    replay_defense: ReplayDefense | None


class WebhookVerifyFailure(Exception):  # noqa: N818 - a door outcome, raised and rendered like an error
    """A verification that did not pass; the door answers ``status`` with the constant ``message``."""

    __tai_error_kind__ = ErrorKind.UNAUTHORIZED

    def __init__(self, status: Literal[401, 405, 500], message: str) -> None:
        """Carry the door's ``status`` and its constant public ``message``; the error kind follows the status."""
        super().__init__(message)
        self.status = status
        self.message = message
        self.__tai_error_kind__ = _KIND_BY_STATUS[status]


async def verify_webhook(
    binding: TopicVerifierBinding,
    raw: bytes,
    headers: Mapping[str, str],
    *,
    method: str,
    subject: str,
    with_replay_defense: bool,
) -> WebhookVerdict:
    """Verify ``raw`` under ``binding``'s verifier; raise :class:`WebhookVerifyFailure` on any failure.

    Fails closed: an unknown verifier name, a verifier bug or a missing secret is a 500; a
    signature (or replay-header) failure is a 401; a GET on a body-signature (``post_only``)
    verifier is a 405. ``subject`` names the request in log lines only (a sanitised topic or
    ``interaction <id>``). The replay declaration is computed only when ``with_replay_defense``.
    """
    try:
        verifier = tai42_app.webhook_verifiers.get(binding.verifier)
    except Exception:
        # A bound name that no longer resolves must deny, never dispatch unverified.
        logger.exception("webhook verify: unknown verifier %r for %s", binding.verifier, subject)
        raise WebhookVerifyFailure(500, VERIFY_ERROR) from None
    post_only = bool(getattr(verifier, "post_only", False))
    if post_only and method == "GET":
        # A body-signature verifier signs the raw body; a GET would sign an empty body while
        # the real payload rides the URL unauthenticated.
        raise WebhookVerifyFailure(405, POST_ONLY)
    try:
        await verifier.verify(raw, headers, binding.config)
        # Computed over the SAME bytes only after a passing verify: a missing delivery-id
        # header raises WebhookVerificationError (a 401), a binding misconfiguration a 500.
        defense = verifier.replay_defense(raw, headers, binding.config) if with_replay_defense else None
    except WebhookVerificationError as exc:
        logger.warning("webhook verification failed for %s: %s", subject, exc)
        raise WebhookVerifyFailure(401, VERIFY_FAILED) from None
    except Exception:
        logger.exception("webhook verifier error for %s", subject)
        raise WebhookVerifyFailure(500, VERIFY_ERROR) from None
    return WebhookVerdict(post_only=post_only, replay_defense=defense)
