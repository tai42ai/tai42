"""The inbound-media retention reaper — the SOLE deleter of an ingested blob + its metadata.

A run-until-cancelled loop that walks the durable ``media-meta:expiry`` index for every ``media_id``
past its horizon (a PENDING upload never bound to a message, past ``pending_ttl_seconds``; a BOUND
record past the flat record horizon) and, per member, deletes the blob, then the metadata hash, then
the expiry member — in THAT order, so a crash mid-delete is re-driven on the next pass and never leaves
an orphan. The blob path is a pure function of the ``media_id`` (``inbound_media_blob_path``), so the
reaper derives it from the durable index member alone and needs NO metadata hash read — a member whose
hash is already gone is still reclaimed. Nothing else deletes a blob / meta hash / expiry
member: the hash carries no Redis TTL, so this loop closes the orphaned-blob gap that an object
store's lack of TTL would otherwise leave.

A per-member delete fault that is NOT a missing blob is logged loudly AND evented, and the member is
LEFT in the index for the next pass — a DEFINED retry, never a swallow. A whole-scan fault (the index
read itself) propagates. The loop is a no-op each pass when the conversations store is unconfigured or
no blob provider is registered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

from tai42_contract.app import tai42_app

from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore, inbound_media_blob_path
from tai42_skeleton.settings.media_ingest import media_ingest_settings

if TYPE_CHECKING:
    from tai42_contract.storage import Storage

    from tai42_skeleton.app.facets import StorageFacet

logger = logging.getLogger(__name__)

# The platform-event topic emitted when a due member's blob delete fails with anything other than a
# missing blob: the member is kept for the next pass. Core states the fact; a deployment wires a hook
# on this topic to decide what an operator sees. Best-effort, like the interactions reaper's events;
# the payload names the ``media_id`` only, never the blob or a credential.
MEDIA_REAP_FAILED_EVENT_TOPIC = "conversations_media_reap_failed"


def _storage_provider() -> Storage | None:
    # The registered blob provider off the skeleton StorageFacet (the contract AppStorage Protocol has
    # no ``provider`` member), None while dead by default — the reaper is the sole deleter of the blob.
    return cast("StorageFacet", tai42_app.storage).provider


async def _emit_media_reap_failed(media_id: str) -> None:
    """Emit ``conversations_media_reap_failed`` ONCE for a due member whose blob delete faulted.

    Best-effort: a hooks-manager failure is logged and swallowed so it never perturbs the pass — the
    member is already kept for the next pass when this runs. The payload names the ``media_id`` only.
    """
    from tai42_skeleton.hooks.cache import get_hooks_manager

    try:
        await get_hooks_manager().on_event(topic=MEDIA_REAP_FAILED_EVENT_TOPIC, payload={"media_id": media_id})
    except Exception:
        logger.warning(
            "media retention reaper: failed to emit %r for media %s",
            MEDIA_REAP_FAILED_EVENT_TOPIC,
            media_id,
            exc_info=True,
        )


async def _reap_one_media(provider: Storage, store: InboundMediaMetaStore, media_id: str) -> None:
    """Delete one due member's blob (if present) then its metadata hash + expiry member, idempotently.

    A missing blob (``FileNotFoundError``) is the contract's own idempotent-caller instruction: it means
    the blob is already absent (a re-driven crash, or a record whose upload never landed), so the record
    is still dropped. Any OTHER exception from the blob delete propagates to the per-member guard, which
    keeps the member for the next pass — the blob delete precedes the record delete, so the member is
    never removed while its blob may still exist.
    """
    storage_path = inbound_media_blob_path(media_id)
    try:
        await provider.delete(storage_path)
    except FileNotFoundError:
        logger.warning("media retention reaper: blob %s already absent; dropping its metadata record", storage_path)
    await store.delete(media_id)


async def reap_expired_media_once() -> int:
    """Run one reaper pass, deleting up to the due-scan limit of media past its horizon; return how many were reaped.

    A no-op (returns 0) when the conversations store is unconfigured (no durable index to walk) or no
    blob provider is registered (nothing to delete through). The index read is outside the per-member
    guard, so a scan-level fault propagates; a per-member delete fault is logged + evented and its member
    kept for the next pass.
    """
    manager = get_conversations_manager()
    if not manager.durable:
        return 0
    provider = _storage_provider()
    if provider is None:
        return 0
    store = manager.media_meta
    reaped = 0
    now = time.time()
    for media_id in await store.due_expiry_ids(now):
        # Guard each member: one member whose blob delete deterministically faults must not abort the
        # whole pass and strand the others. Log loudly, event it, keep it for the next pass, continue.
        try:
            await _reap_one_media(provider, store, media_id)
            reaped += 1
        except Exception:
            logger.error(
                "media retention reaper failed deleting media %s; leaving it in the index for the next pass",
                media_id,
                exc_info=True,
            )
            await _emit_media_reap_failed(media_id)
    return reaped


async def run_media_reaper_loop() -> None:
    """Run the media retention reaper until cancelled.

    Each pass sleeps the configured interval, then reaps every media past its horizon. A per-pass error
    is logged loudly and the loop survives to the next interval — a silently dead reaper would orphan
    every abandoned pending upload and every expired bound blob, the exact failures this loop removes —
    while a cancellation (shutdown) propagates for a clean exit.
    """
    while True:
        await asyncio.sleep(media_ingest_settings().reaper_interval_seconds)
        try:
            reaped = await reap_expired_media_once()
            if reaped:
                logger.info("media retention reaper reaped %d media item(s)", reaped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("media retention reaper pass failed; retrying next interval", exc_info=True)
