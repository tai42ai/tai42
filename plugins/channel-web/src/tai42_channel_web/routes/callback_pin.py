"""The pin a web door applies to a stored callback URL before it forwards anything to it.

A question record's ``callback_url`` is a bearer capability the platform minted. Before an answer or
a reaction is POSTed to it, it is pinned against the platform's callback prefix: a record that is
not exactly the minted shape (or a deployment with no public base to pin against) is refused with an
ERROR log naming the interaction, the reason and the host — never the URL, which embeds the ticket —
and the refusal is raised, so the door answers 500 and nothing is sent.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from tai42_contract.app import tai42_app
from tai42_kit.interactions import CallbackUrlRefusedError, pin_callback_url

logger = logging.getLogger(__name__)

__all__ = ["CallbackPrefixUnsetError", "pin_stored_callback"]


class CallbackPrefixUnsetError(RuntimeError):
    """The deployment configures no public base, so the platform mints no callback URL to pin against."""


def pin_stored_callback(callback_url: str, *, interaction_id: str, forwarded: str) -> None:
    """Pin ``callback_url`` against the platform's callback prefix, or log at ERROR and raise.

    ``forwarded`` names what the door would have sent (``answer`` / ``reaction``), for the log line.
    Raises :class:`CallbackUrlRefusedError` for a URL that is not the minted shape and
    :class:`CallbackPrefixUnsetError` when there is no prefix to pin against.
    """
    prefix = tai42_app.interactions.callback_url_prefix()
    if prefix is None:
        _log_refusal(interaction_id, "public_base_unset", _host_of(callback_url), forwarded)
        raise CallbackPrefixUnsetError(
            "INTERACTIONS_PUBLIC_BASE_URL is not set; the platform mints no callback URL to pin against"
        )
    try:
        pin_callback_url(callback_url, prefix=prefix)
    except CallbackUrlRefusedError as exc:
        refused = exc
    else:
        return
    _log_refusal(interaction_id, refused.reason.value, refused.target_host, forwarded)
    raise refused


def _host_of(callback_url: str) -> str | None:
    try:
        return urlsplit(callback_url).hostname
    except ValueError:
        return None


def _log_refusal(interaction_id: str, reason: str, target_host: str | None, forwarded: str) -> None:
    logger.error(
        "web %s: stored callback for interaction %s refused (%s, host %r); the %s is not forwarded",
        forwarded,
        interaction_id,
        reason,
        target_host,
        forwarded,
    )
