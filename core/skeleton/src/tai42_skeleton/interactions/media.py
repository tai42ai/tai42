"""Store the media a question or notification attaches BY REFERENCE.

A ``data:image/*`` URI a caller attaches is decoded once and held under a random
capability id; the durable record and the channel send carry a served reference
(``MEDIA_ROUTE_PREFIX{id}``) instead of the inline bytes. The bytes ride the Redis
hash as base64 text so they round-trip a ``decode_responses`` client. Loud by
contract — a non-image mime or malformed base64 raises, never a silent drop. Key
shapes live on :class:`InteractionStore`; the same key prefix as the rest of the
interactions store.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import Awaitable, Sequence
from typing import cast

from redis.asyncio import Redis
from tai42_contract.interactions import MEDIA_ROUTE_PREFIX, MediaItem, MediaKind, check_media_list

from tai42_skeleton.interactions.store import InteractionStore, as_str

_DATA_IMAGE_PREFIX = "data:image/"
_ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _parse_data_image(data_url: str) -> tuple[str, bytes]:
    # Split a ``data:image/<subtype>;base64,<payload>`` URI into (mime, decoded bytes).
    # A non-image mime, a missing ``;base64`` marker, an unlisted subtype, empty or
    # non-strict-base64 payload raises ValueError — never a silent skip.
    if not data_url.startswith("data:"):
        raise ValueError("media data url must start with 'data:'")
    header, sep, payload = data_url[len("data:") :].partition(",")
    if not sep:
        raise ValueError("media data url is missing the ',' payload separator")
    if not header.endswith(";base64"):
        raise ValueError("media data url must be base64-encoded (a ';base64' marker)")
    mime = header[: -len(";base64")]
    if mime not in _ALLOWED_MIME:
        raise ValueError(f"media data url mime {mime!r} is not an accepted image type")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("media data url payload is not valid base64") from exc
    if not raw:
        raise ValueError("media data url payload is empty")
    return mime, raw


async def store_data_image(store: InteractionStore, r: Redis, data_url: str, ttl_seconds: int) -> str:
    """Decode a ``data:image/*`` URI, hold the bytes under a random id, return the id.

    The id is 43 urlsafe-base64 chars (32 random bytes) — the capability secret the
    served-media route validates. The hash keeps the mime and the image as base64 text
    (binary-safe under a ``decode_responses`` client); the key expires after
    ``ttl_seconds`` unless a later co-grouped ``add`` extends it."""
    mime, raw = _parse_data_image(data_url)
    media_id = secrets.token_urlsafe(32)
    key = store.media_key(media_id)
    pipe = r.pipeline()
    pipe.hset(key, mapping={"mime": mime, "b64": base64.b64encode(raw).decode("ascii")})
    pipe.expire(key, ttl_seconds)
    await pipe.execute()
    return media_id


async def read_media(store: InteractionStore, r: Redis, media_id: str) -> tuple[str, bytes] | None:
    """Return ``(mime, bytes)`` for a stored media id, or None when absent/expired."""
    # redis-py's async stubs type ``hgetall`` with the sync (non-awaitable) return; it
    # is awaitable at runtime.
    data = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(store.media_key(media_id)))
    if not data:
        return None
    mime = as_str(data.get("mime") or data.get(b"mime"))
    b64 = as_str(data.get("b64") or data.get(b"b64"))
    if mime is None or b64 is None:
        raise ValueError(f"stored media {media_id!r} is missing its mime/b64 fields")
    return mime, base64.b64decode(b64)


async def substitute_media(
    store: InteractionStore,
    r: Redis,
    items: Sequence[MediaItem | dict],
    ttl_seconds: int,
    *,
    base_url: str | None = None,
) -> list[MediaItem]:
    """Store every ``data:image/*`` image item by reference, return the list with each
    such item's url swapped for a served reference. Links and https images pass through
    unchanged. Pure over the input (returns a new list; each item is coerced through
    ``MediaItem`` so its input shape is validated before any store). ``base_url`` is
    prepended to make an ABSOLUTE served url for a channel send (a vendor fetches it
    from its own servers); None yields the relative same-origin url the inbox renders."""
    prefix = base_url.rstrip("/") + MEDIA_ROUTE_PREFIX if base_url is not None else MEDIA_ROUTE_PREFIX
    # Coerce (validating each item's shape) then apply the list-level caps BEFORE any
    # store write, so an over-count or over-budget request is refused before a single
    # data: image is decoded and held — the substitution cannot be a cap bypass.
    coerced = [raw if isinstance(raw, MediaItem) else MediaItem.model_validate(raw) for raw in items]
    check_media_list(coerced)
    result: list[MediaItem] = []
    for item in coerced:
        if item.kind is MediaKind.IMAGE and item.url.startswith(_DATA_IMAGE_PREFIX):
            media_id = await store_data_image(store, r, item.url, ttl_seconds)
            item = MediaItem(kind=item.kind, url=prefix + media_id, caption=item.caption)
        result.append(item)
    return result
