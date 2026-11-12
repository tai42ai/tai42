"""The one reader of stored media by its 43-char id, across the two stores the served route serves.

The inbound blob-backed store (the metadata record on the conversations Redis, then the bytes via
the storage provider) is consulted first, else the outbound Redis media store. The served-media
HTTP route and ``app.media.read_media`` both read through :func:`load_served_media`, so the two
apply the same rules. The id is a capability secret: the errors this module raises never carry
it; the operator's warning log names it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    MEDIA_ID_RE,
    MediaKind,
    MediaNotFoundError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.interactions.media import read_media
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore

if TYPE_CHECKING:
    from tai42_contract.storage import Storage

    from tai42_skeleton.app.facets import StorageFacet

logger = logging.getLogger(__name__)

_NOT_FOUND = "stored media not found"


@dataclass(frozen=True, slots=True)
class ServedMediaRecord:
    """One stored media read by its id: its bytes and what the served route answers them with.

    ``kind`` is an inbound item's own kind and :attr:`MediaKind.IMAGE` for an outbound one (that
    store holds pictures only); ``filename`` is set for an inbound item only; ``expires_in`` is
    the seconds left of the media's retention, 0 once past it.
    """

    mime: str
    data: bytes
    kind: MediaKind
    filename: str | None
    expires_in: int


def _blob_provider() -> Storage | None:
    """The registered blob provider off the skeleton storage facet, or ``None`` while dead."""
    return cast("StorageFacet", tai42_app.storage).provider


def _require_within(size: int, max_bytes: int | None) -> None:
    if max_bytes is not None and size > max_bytes:
        raise MediaTooLargeError(f"stored media is {size} bytes, over the {max_bytes}-byte limit")


async def load_served_media(media_id: str, *, max_bytes: int | None) -> ServedMediaRecord:
    """The stored media for ``media_id``, under the served route's rules.

    ``max_bytes`` bounds the size the caller accepts (``None``: no bound); an inbound item is
    measured from its record before its bytes are loaded. Raises ``MediaNotFoundError`` — one
    uniform error for a malformed, unknown or still-pending id, an id whose media is removed, an
    inbound record whose blob is absent, and an unconfigured store, so the absence of a store is
    never told apart —
    ``MediaStoreUnavailableError`` when an inbound record exists and no blob provider is
    registered, and ``MediaTooLargeError`` over ``max_bytes``. Store faults propagate unchanged.
    """
    if MEDIA_ID_RE.fullmatch(media_id) is None:
        raise MediaNotFoundError(_NOT_FOUND)
    inbound = await _load_inbound(media_id, max_bytes=max_bytes)
    if inbound is not None:
        return inbound
    return await _load_outbound(media_id, max_bytes=max_bytes)


async def _load_inbound(media_id: str, *, max_bytes: int | None) -> ServedMediaRecord | None:
    # A present record answers here; only the absence of a record (or an unconfigured
    # conversations store) falls through to the outbound store. A pending item is not found: a
    # capability id must not leak bytes before the owning message binds it.
    manager = get_conversations_manager()
    if not manager.durable:
        return None
    store = manager.media_meta
    meta = await store.get(media_id)
    if meta is None:
        return None
    if meta.pending:
        raise MediaNotFoundError(_NOT_FOUND)
    _require_within(meta.size, max_bytes)
    provider = _blob_provider()
    if provider is None:
        logger.warning("inbound media %s has a metadata record but no blob provider is registered", media_id)
        raise MediaStoreUnavailableError("media store unavailable")
    try:
        payload = await provider.load_bytes(meta.storage_path)
    except FileNotFoundError:
        logger.warning("inbound media %s record present but its blob %r is absent", media_id, meta.storage_path)
        # Not chained: the provider's error names the blob path, which carries the id.
        raise MediaNotFoundError(_NOT_FOUND) from None
    score = await store.score(media_id)
    expires_in = 0 if score is None else max(0, int(score - time.time()))
    return ServedMediaRecord(
        mime=meta.mime, data=payload, kind=meta.kind, filename=meta.filename, expires_in=expires_in
    )


async def _load_outbound(media_id: str, *, max_bytes: int | None) -> ServedMediaRecord:
    if not interactions_store_configured():
        raise MediaNotFoundError(_NOT_FOUND)
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        found = await read_media(store, r, media_id)
        if found is None:
            raise MediaNotFoundError(_NOT_FOUND)
        mime, payload = found
        _require_within(len(payload), max_bytes)
        # The bytes vanish at the key's TTL (extended to the owning group's horizon).
        remaining_ttl = await r.ttl(store.media_key(media_id))
    return ServedMediaRecord(
        mime=mime, data=payload, kind=MediaKind.IMAGE, filename=None, expires_in=max(0, remaining_ttl)
    )
