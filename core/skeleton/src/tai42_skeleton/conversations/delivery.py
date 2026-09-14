"""The delivery executor — sends a produced answer back and drives its record to a terminal
state, exactly once.

``door=channel`` chunks the answer through the channel's ``notify`` (:mod:`.delivery_channel`),
``door=api`` POSTs it under an HMAC signature or serves a poll-only row (:mod:`.delivery_api`).
Every send is guarded by an atomic per-record leased claim. This module owns the per-record
entrypoint (:func:`deliver`), the out-of-band receipt sink, task spawning, and the two
boot/recovery operations (:func:`redrive_pending`, :func:`sweep_stalled_deliveries`) that
re-drive individual stranded records; the process-wide periodic task that invokes them lives in
:mod:`.delivery_sweep`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from uuid import uuid4

from tai42_contract.app import tai42_app
from tai42_contract.conversations import DeliveryReceipt

from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.delivery_api import _deliver_api, _post_callback
from tai42_skeleton.conversations.delivery_channel import _deliver_channel
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

logger = logging.getLogger(__name__)

# Signed api-door callback signature prefix: ``sha256=<hex digest>``.
_SIGNATURE_PREFIX = "sha256="

# Strong references to in-flight delivery / grace tasks so they are not GC'd mid-flight.
_DELIVERY_TASKS: set[asyncio.Task[None]] = set()


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def _backoff_seconds(settings: ConversationsSettings, attempt: int) -> float:
    """Exponential backoff for retry ``attempt`` (1-based): ``base * 2**(attempt-1)``
    capped at the configured maximum."""
    raw = settings.delivery_backoff_base_seconds * (2 ** (attempt - 1))
    return min(raw, settings.delivery_backoff_max_seconds)


def split_message(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into ordered chunks of at most ``max_chars``, breaking at the last
    newline or space in the window when there is one. The concatenation of the chunks is
    exactly ``text`` — nothing is dropped or reordered."""
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = max(window.rfind("\n"), window.rfind(" "))
        # Hard-cut when a whitespace break would leave an empty head, so a run of
        # non-breakable characters still makes progress.
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


# -- one-record delivery -----------------------------------------------------


async def deliver(message_id: str) -> None:
    """Drive one record's delivery to a terminal or provisional state, exactly once.

    Takes the atomic claim FIRST, so a record already terminal, gone, or under another
    worker's live lease is left untouched. A record still at intake carries no answer and
    refuses loudly."""
    store = _store()
    token = uuid4().hex
    claimed = await store.claim_delivery(message_id, time.time(), token, store.settings.delivery_claim_lease_seconds)
    if claimed == -2:
        raise RuntimeError(
            f"conversation record {message_id!r} is still at intake and carries no answer; the delivery machine "
            "must not be driven on it"
        )
    if claimed != 1:
        # Not an error: terminal, gone, or held by another worker's live lease.
        logger.debug("conversations: record %s was not claimed for delivery (claim returned %d)", message_id, claimed)
        return
    record = await store.get_record(message_id)
    if record is None:
        # Deleted between the claim and this re-read.
        return
    if record.delivery_status is not DeliveryStatus.PENDING_DELIVERY:
        # The claim admits only pending_delivery; a provisional record is fully sent and
        # awaits a receipt. Reaching here means the claim and this guard disagree.
        raise RuntimeError(
            f"conversation record {message_id!r} is {record.delivery_status.value}, not pending_delivery, after "
            "winning the delivery claim; the delivery machine must not re-send it"
        )
    if record.attempts > 0:
        # Earlier attempts on a non-terminal record: this claim is a takeover.
        logger.info(
            "conversations: re-driving record %s (door=%s) after %d unfinished attempt(s)",
            record.message_id,
            record.door,
            record.attempts,
        )
    if record.attempts >= store.settings.delivery_max_attempts:
        # Attempts spent without a terminal state: fail it so the sweep stops re-driving it.
        outcome = await store.mark_failed(record.message_id, record.attempts, time.time(), token)
        logger.error(
            "conversations: record %s exhausted %d delivery attempt(s) without a terminal outcome "
            "(failed write returned %d)",
            record.message_id,
            record.attempts,
            outcome,
        )
        return
    if record.door == "channel":
        await _deliver_channel(store, record, token)
    else:
        await _deliver_api(store, record, token)


async def _confirm_after_grace(message_id: str, grace_seconds: float) -> None:
    """Confirm a still-``provisional`` record ``delivered`` once its grace window elapses.
    The atomic ingest is a no-op on a record a receipt already made terminal."""
    await asyncio.sleep(grace_seconds)
    await _store().ingest_receipt(message_id, DeliveryReceipt.DELIVERED, time.time())


# -- out-of-band receipt sink ------------------------------------------------


async def record_delivery_status(channel: str, provider_message_id: str, status: DeliveryReceipt) -> None:
    """Ingest a channel's out-of-band receipt for an outbound message.

    Resolves ``provider_message_id`` through the outbound reverse index and applies the
    receipt atomically. Raises when the id maps to no record (unknown or already swept), and
    when it names a record whose send is still in flight — a receipt for one chunk may not
    terminalise a record whose remaining chunks are still going out."""
    store = _store()
    message_id = await store.resolve_outbound(channel, provider_message_id)
    if message_id is None:
        raise LookupError(
            f"conversations: delivery receipt names outbound id {provider_message_id!r} on channel {channel!r} "
            "that maps to no answer record"
        )
    outcome = await store.ingest_receipt(message_id, status, time.time())
    if outcome == -1:
        raise LookupError(
            f"conversations: outbound id {provider_message_id!r} resolved to record {message_id} which no longer exists"
        )
    if outcome == -3:
        raise RuntimeError(
            f"conversations: delivery receipt {status.value} for outbound id {provider_message_id!r} names record "
            f"{message_id}, whose send has not finished; the record keeps its in-flight state"
        )
    if outcome == -2:
        logger.error(
            "conversations: delivery receipt %s for record %s conflicts with an already-terminal state; ignored",
            status.value,
            message_id,
        )
    elif status is DeliveryReceipt.FAILED and outcome == 1:
        logger.error("conversations: outbound delivery of record %s reported FAILED by the channel", message_id)


async def mark_wait_delivered(message_id: str) -> bool:
    """Confirm a record ``delivered`` because the API door's sync wait returned its answer;
    no callback is POSTed. Takes the same atomic claim a background delivery would, so only
    one of the two paths delivers. Returns ``True`` when this call is that one."""
    store = _store()
    token = uuid4().hex
    claimed = await store.claim_delivery(message_id, time.time(), token, store.settings.delivery_claim_lease_seconds)
    if claimed != 1:
        return False
    outcome = await store.mark_delivered(message_id, [], await store.bump_attempt(message_id), time.time(), token)
    return outcome == 1


# -- boot re-drive + per-record stall recovery -------------------------------


async def redrive_pending() -> None:
    """Resume every non-terminal record on boot so nothing is stranded: re-deliver a
    ``pending_delivery`` record through the exactly-once claim, confirm a ``provisional``
    one past its grace, and reschedule the fallback confirmation for one still inside it.

    Only boot can rebuild the in-process grace timers lost with the previous process, so
    the rescheduling lives here and not in the periodic sweep."""
    store = _store()
    now = time.time()
    for work in await store.pending_work():
        if work.delivery_status is DeliveryStatus.PENDING_DELIVERY:
            _spawn(deliver(work.message_id))
        elif work.delivery_status is DeliveryStatus.PROVISIONAL:
            if work.grace_deadline is not None and now >= work.grace_deadline:
                await store.ingest_receipt(work.message_id, DeliveryReceipt.DELIVERED, now)
            elif work.grace_deadline is not None:
                _spawn(_confirm_after_grace(work.message_id, work.grace_deadline - now))
            else:
                _spawn(_confirm_after_grace(work.message_id, store.settings.delivery_grace_seconds))


async def sweep_stalled_deliveries() -> None:
    """One pass over the records a dead worker could have stranded.

    The exactly-once claim is itself the lease-expiry test, so a re-driven
    ``pending_delivery`` record is only ever a genuinely abandoned one. A ``provisional``
    record past its grace is confirmed here because the in-process fallback confirmation
    died with the worker that scheduled it."""
    store = _store()
    now = time.time()
    for work in await store.pending_work():
        if work.delivery_status is DeliveryStatus.PENDING_DELIVERY:
            _spawn(deliver(work.message_id))
        elif (
            work.delivery_status is DeliveryStatus.PROVISIONAL
            and work.grace_deadline is not None
            and now >= work.grace_deadline
        ):
            await store.ingest_receipt(work.message_id, DeliveryReceipt.DELIVERED, now)


# -- task spawning -----------------------------------------------------------


def spawn_delivery(message_id: str) -> None:
    """Spawn the background delivery of an already-persisted record (fire-and-forget)."""
    _spawn(deliver(message_id))


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _DELIVERY_TASKS.add(task)
    task.add_done_callback(_on_task_done)


def _on_task_done(task: asyncio.Task[None]) -> None:
    _DELIVERY_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("conversations: delivery task failed", exc_info=exc)


__all__ = [
    "_post_callback",
    "deliver",
    "get_conversations_manager",
    "mark_wait_delivered",
    "record_delivery_status",
    "redrive_pending",
    "spawn_delivery",
    "split_message",
    "sweep_stalled_deliveries",
    "tai42_app",
]
