"""The stored-entry reader: one stored object as a text, document or binary entry.

Both entry doors (the storage door and the templates door) call
:func:`read_stored_entry`. The content classifier (``classify``) decides the kind once,
on the object's sample, and a text or document entry's content is what
``load_classified`` loads through that decision: no other code here decides. A binary
object is classified from its stat and at most two detection windows, never read
whole. An object within two windows is sampled whole, so its sample is its bytes; a
larger text or document object is read whole once more, bounded by the content cap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tai42_contract.storage import ObjectStat, Storage

from tai42_skeleton.operations import BadRequestError, NotFoundError
from tai42_skeleton.operations.storage_provider import require_storage_provider
from tai42_skeleton.operations.store_keys import checked_store_key
from tai42_skeleton.template.errors import ContentNotLoadableError


def _not_found(resource_id: str) -> NotFoundError:
    return NotFoundError(f"resource {resource_id!r} not found")


def _not_loadable(resource_id: str, exc: ContentNotLoadableError) -> BadRequestError:
    return BadRequestError(f"resource {resource_id!r} cannot be loaded: {exc}")


async def _stat_and_sample(provider: Storage, resource_id: str, window: int) -> tuple[ObjectStat, bytes]:
    """The object's stat and the bytes the classifier sees: the whole object up to two windows, else both ends."""
    try:
        stat = await provider.stat(resource_id)
        size = stat.size
        if size == 0:
            return stat, b""
        if size <= 2 * window:
            return stat, await provider.load_range(resource_id, 0, size)
        head = await provider.load_range(resource_id, 0, window)
        return stat, head + await provider.load_range(resource_id, size - window, window)
    except FileNotFoundError as exc:
        raise _not_found(resource_id) from exc
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        raise BadRequestError(str(exc)) from exc


async def _load_whole(provider: Storage, resource_id: str, size: int, sample: bytes) -> bytes:
    """The whole object's bytes, read once the classifier keeps it as text or a document.

    A sample of ``size`` bytes is the whole object already and is returned as it is.
    Otherwise the read is bounded by ``FILE_LOADING_MAX_BYTES``, so an object that grew
    past the cap after its stat is refused on the bytes actually read and is never read
    whole.
    """
    if len(sample) == size:
        return sample
    from tai42_skeleton.template.file_loading import read_within_cap

    try:
        return await read_within_cap(provider, resource_id)
    except FileNotFoundError as exc:
        raise _not_found(resource_id) from exc


async def read_stored_entry(resource_id: str) -> dict[str, Any]:
    """Read ``resource_id`` as ``{"kind", "id", "content_type", "size"}``, plus ``content`` for text and documents.

    Raises ``BadRequestError`` for a non-canonical key, a provider boundary ``ValueError``,
    and content the classifier refuses (over the cap, non-UTF-8 text, an extractor's
    failure); ``NotFoundError`` for a missing object; ``NotSupportedError`` with no
    provider, and without the ``files`` extra (before any storage call). Any other
    provider failure propagates.
    """
    checked_store_key(resource_id)
    provider = require_storage_provider()
    from tai42_skeleton.template.file_loading import (
        DETECTION_WINDOW_BYTES,
        check_within_cap,
        classify,
        load_classified,
    )

    stat, sample = await _stat_and_sample(provider, resource_id, DETECTION_WINDOW_BYTES)
    entry: dict[str, Any] = {"id": resource_id, "content_type": stat.content_type, "size": stat.size}
    classified = await asyncio.to_thread(classify, sample, resource_id)
    if classified.kind == "binary":
        return {"kind": "binary", **entry}
    try:
        check_within_cap(stat.size)
        data = await _load_whole(provider, resource_id, stat.size, sample)
        content = await asyncio.to_thread(load_classified, data, classified, resource_id)
    except ContentNotLoadableError as exc:
        raise _not_loadable(resource_id, exc) from exc
    return {"kind": classified.kind, **entry, "content": content}
