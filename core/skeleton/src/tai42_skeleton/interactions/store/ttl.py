"""Deadline, TTL and media-id computation from a request.

The millisecond clocks, the served-media id extraction, and the per-key TTL an async
park's horizon needs.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tai42_contract.interactions import InteractionRequest, served_media_id


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _created_ms(request: InteractionRequest) -> int:
    return int(request.created_at.timestamp() * 1000)


def _timeout_ms(request: InteractionRequest) -> int:
    return int(request.timeout_at.timestamp() * 1000)


def _expiry_ms(request: InteractionRequest) -> int:
    # Only called for an async request whose ``expiry_at`` is set (guarded at the
    # call site); a None here is a caller bug, never a silent 0.
    if request.expiry_at is None:
        raise ValueError("_expiry_ms called on a request with no expiry_at")
    return int(request.expiry_at.timestamp() * 1000)


# Fallback ``expiry_ttl_margin_seconds`` for a direct ``add`` caller (the helper
# always passes a reaper-derived value); a park with an ``expiry_at`` within
# ``idle_ttl`` never reaches this since its TTL floors to ``idle_ttl``.
_DEFAULT_EXPIRY_TTL_MARGIN_SECONDS = 60


def _media_ids_of(request: InteractionRequest) -> list[str]:
    # The stored-media ids a request's media references — items whose url is a served
    # reference; https/link/data: items carry none. A data:image is substituted to a
    # served reference before the request is built, so by the time it reaches ``add``
    # only served references remain to index. Both served forms count: the same-origin
    # relative ``{MEDIA_ROUTE_PREFIX}{id}`` an inbox-only ask stores, and the absolute
    # ``public_base_url``-minted reference a channel ask stores — ``served_media_id``
    # extracts the id from either, so a channel ask's bytes key is joined to the group
    # index and TTL-extended past bootstrap idle, never left to 404 while the park lives.
    if not request.media:
        return []
    return [mid for item in request.media if (mid := served_media_id(item.url)) is not None]


def _key_ttl(request: InteractionRequest, idle_ttl: int, now_ms: int, expiry_margin_s: int) -> int:
    """Return the TTL a question's own keys need.

    An async park with an ``expiry_at`` beyond the idle horizon must survive to its expiry
    PLUS a reaper-pass margin, or its state hash would expire before the reaper reads it —
    leaving the question unanswerable in the ``idle_ttl``..``expiry_at`` gap and stranding
    the continuation. Every other question (sync, or a park expiring within the idle horizon)
    uses the flat ``idle_ttl``.
    """
    if request.mode == "async" and request.expiry_at is not None:
        horizon = math.ceil((_expiry_ms(request) - now_ms) / 1000) + expiry_margin_s
        return max(idle_ttl, horizon)
    return idle_ttl
