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


def _key_ttl(request: InteractionRequest, idle_ttl: int, now_ms: int) -> int:
    """Return the TTL a question's own keys need.

    An async park with an ``expiry_at`` keeps its keys one retention horizon (``idle_ttl``) past
    its deadline: the seconds remaining to ``expiry_at`` (none when already past it) plus
    ``idle_ttl``. The expiry reaper resolves the park however late it runs within that horizon,
    the same horizon every durable record of the reaper keeps. Every other question uses the flat
    ``idle_ttl``.
    """
    if request.mode == "async" and request.expiry_at is not None:
        return max(0, math.ceil((_expiry_ms(request) - now_ms) / 1000)) + idle_ttl
    return idle_ttl
