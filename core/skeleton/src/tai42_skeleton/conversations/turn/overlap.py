"""The overlap chokepoint: the per-thread policy decision every message turn passes.

Every message turn reaches :func:`resolve_batch` after the FIFO lock is held and before its
target runs (``schedule.py`` ``_run``, under ``run_reserved``) — the ONE place the route's
:class:`~tai42_contract.conversations.OverlapPolicy` is applied. It re-reads the lead, waits
out the settle window, gathers the followers accepted since the lead, merges them under
``deliver="all"``, and carries the superseded records whose successor rides this batch. A
``running="cancel"`` route additionally arms :func:`cancel_watch`, a task polling the shared
per-thread cancel marker that cancels the running turn in favour of a newer message.

The policy governs participant MESSAGE turns only (``origin="client"``,
``inbound_kind="message"``); an event turn is excluded by kind at the gather and carries no
marker. The default policy (``continue``/``one``/``0``) takes the fast path — a single-member
batch with no gather, no marker, no watcher — so every path stays byte-identical.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from tai42_contract.conversations import ConversationRoute, TurnSupersededError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn import record as record_module
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger("tai42_skeleton.conversations.turn")


@dataclass(frozen=True)
class Batch:
    """The messages one turn carries, resolved at the chokepoint.

    ``lead`` is the message whose turn runs; ``members`` is the lead followed by the followers
    merged into it (just the lead under ``deliver="one"``), in acceptance order; ``superseded``
    is the earlier records dropped in favour of this batch (empty under ``deliver="one"``), in
    acceptance order. The whole batch is bounded by the thread's FIFO depth.
    """

    lead: ConversationRecord
    members: list[ConversationRecord]
    superseded: list[ConversationRecord] = field(default_factory=list)

    @property
    def newest(self) -> tuple[float, str]:
        """The thread-index key of the batch's newest member — ``(created_at, message_id)``.

        The watcher cancels the turn only for a marker strictly greater than this, so a marker
        naming a member (or an older message) never cancels the batch that already carries it.
        """
        return max((member.created_at, member.message_id) for member in self.members)


def is_message_turn(record: ConversationRecord) -> bool:
    """Whether the overlap policy governs this record — a participant message turn.

    ``origin="client"`` and ``inbound_kind="message"``: an event turn and an operator send are
    excluded, so neither is merged, superseded, nor a canceller. The single predicate every
    overlap gate shares — the batch decision, the cancel marker and the cancel watcher — so no
    door can arm the policy for a turn it does not govern.
    """
    return record.origin == "client" and record.inbound_kind == "message"


async def resolve_batch(route: ConversationRoute, intake: ConversationRecord) -> Batch | None:
    """The batch ``intake``'s turn carries, or ``None`` when the lead is no longer ``accepted``.

    Applies the overlap decision: re-read the lead (``None`` when an earlier merge/supersede already
    moved it off ``accepted`` — its task exits without a turn), wait out the settle window from
    the lead's acceptance, gather the followers accepted after it, merge them under
    ``deliver="all"`` (a follower already gone is skipped), and carry the superseded records
    whose successor rides this batch. Under ``deliver="one"`` no follower is ever merged, so the
    lead is always still ``accepted`` and the batch is the lead alone.
    """
    policy = route.overlap
    store = accessors._store()
    if not is_message_turn(intake):
        # An event turn (and any non-message turn) runs as its own turn: never merged, never
        # superseded, never a canceller. The single-member batch keeps the payload byte-identical.
        return Batch(lead=intake, members=[intake])
    if policy.deliver != "all":
        # No gather/merge/carry: the turn carries exactly the lead, and no follower is ever
        # merged, so the lead cannot have left ``accepted`` before its own turn.
        await _settle(policy, intake)
        return Batch(lead=intake, members=[intake])
    lead = await store.get_record(intake.message_id)
    if lead is None or lead.delivery_status is not DeliveryStatus.ACCEPTED:
        # An earlier decision merged or superseded this record; its turn does not run.
        return None
    await _settle(policy, lead)
    followers = await store.accepted_after(lead.route_name, lead.thread_id, lead.created_at)
    members = [lead]
    for follower in followers:
        merged = record_module._merged_record(follower, lead.message_id)
        if await _persist_overlap(store, merged) == 1:
            members.append(follower)
        # A follower already off ``accepted`` (a race with its own turn or an earlier decision)
        # returns 0/-1 and is skipped: it is not this batch's to carry.
    superseded = await _gather_superseded(store, lead, {member.message_id for member in members})
    return Batch(lead=lead, members=members, superseded=superseded)


async def _settle(policy, lead: ConversationRecord) -> None:
    """Hold the turn until ``settle_seconds`` have passed since the lead was accepted.

    A fixed window from the lead's ``created_at`` (never sliding, so the wait is bounded and a
    stream cannot starve it); ``settle_seconds == 0`` waits not at all. The intake lease keeps
    refreshing during the wait (``schedule.py`` ``_intake_lease_held`` wraps this call).
    """
    if policy.settle_seconds == 0:
        return
    remaining = policy.settle_seconds - (time.time() - lead.created_at)
    if remaining > 0:
        await asyncio.sleep(remaining)


async def _gather_superseded(store, lead: ConversationRecord, member_ids: set[str]) -> list[ConversationRecord]:
    """The thread's ``superseded`` records whose successor rides this batch, in acceptance order.

    A record dropped by the cancel watcher names its successor; when that successor is a member
    of this batch (the turn that took its place), it rides the payload's ``superseded`` so its
    text is not lost. Read from the thread's own index, bounded by the FIFO depth.
    """
    page = await store.list_thread_records(
        lead.route_name, lead.thread_id, offset=0, limit=store.settings.thread_queue_depth, newest_first=True
    )
    carried = [r for r in page.records if _is_superseded(r) and r.successor_id in member_ids]
    carried.sort(key=lambda r: (r.created_at, r.message_id))
    return carried


def _is_superseded(record: ConversationRecord) -> bool:
    """Whether ``record`` sits at a superseded overlap outcome on either door."""
    if record.delivery_status is DeliveryStatus.SUPERSEDED:
        return True
    return record.delivery_status is DeliveryStatus.PENDING_DELIVERY and record.answer_status == "superseded"


async def _persist_overlap(store, record: ConversationRecord) -> int:
    """Persist a merged/superseded ``record`` through the door-appropriate transition.

    Channel door: the terminal ``merge_record``/``supersede_record`` (nothing is ever sent).
    API door: ``complete_turn`` to ``pending_delivery`` carrying the ``merged``/``superseded``
    ``answer_status`` and successor, delivered as a marker exactly as a ``silent`` outcome is.
    Returns 1 transitioned, 0 no longer at intake, -1 gone.
    """
    if record.door == "channel":
        if record.delivery_status is DeliveryStatus.MERGED:
            return await store.merge_record(record)
        return await store.supersede_record(record)
    return await store.complete_turn(record)


async def supersede_lead(lead: ConversationRecord, successor_id: str) -> ConversationRecord:
    """Resolve ``lead`` as ``superseded`` by ``successor_id`` — no reply, no error, no delivery.

    The outcome a cancelled turn ends in: the record moves straight to its door's superseded
    terminal (channel) or to ``pending_delivery`` carrying the ``superseded`` marker (API). The
    successor is the message that took this turn's place. Raises if the record already left
    intake — an outcome was written elsewhere and this supersede would overwrite it.
    """
    store = accessors._store()
    superseded = record_module._superseded_record(lead, successor_id)
    outcome = await _persist_overlap(store, superseded)
    if outcome != 1:
        raise RuntimeError(
            f"conversations: record {lead.message_id} is no longer at intake "
            f"(supersede answered {outcome}); its outcome was resolved elsewhere and the supersede is discarded"
        )
    return superseded


def turn_text(route: ConversationRoute, batch: Batch, lead_text: str) -> str:
    r"""The whole text this turn runs on.

    Under ``deliver="one"`` it is the lead's own rendered text, byte-identical to today. Under
    ``deliver="all"`` it is the superseded texts then the batch texts, in acceptance order,
    joined with a blank line — so an agent target, a manual-mode append and a no-``payload_expr``
    tool all see the whole turn with no new seam.
    """
    if route.overlap.deliver != "all":
        return lead_text
    texts = [r.inbound_text for r in batch.superseded] + [r.inbound_text for r in batch.members]
    return "\n\n".join(text for text in texts if text.strip())


# -- the cancel marker + watcher ---------------------------------------------


@dataclass(frozen=True)
class _CancelMarker:
    """The per-thread cancel marker's payload — the newest accepted message on the thread."""

    message_id: str
    created_at: float


async def set_cancel_marker(record: ConversationRecord) -> None:
    """Set the thread's cancel marker to ``record`` — every accept on a ``cancel`` message route.

    ``<prefix>:overlap:cancel:{thread_id}`` = ``{message_id, created_at}``, TTL the thread lease.
    Set unconditionally (the watcher decides relevance by comparing to its batch), so a running
    turn learns of every newer message. A worker with no conversations Redis writes nothing.
    """
    settings = accessors._store().settings
    if settings.in_memory:
        return
    key = settings.overlap_cancel_key(record.thread_id)
    value = json.dumps({"message_id": record.message_id, "created_at": record.created_at})
    async with client_ctx(RedisClient, settings.redis) as r:
        await awaited(r.set(key, value, px=settings.thread_lease_seconds * 1000))


async def _read_cancel_marker(settings: ConversationsSettings, key: str) -> _CancelMarker | None:
    async with client_ctx(RedisClient, settings.redis) as r:
        raw = await awaited(r.get(key))
    if raw is None:
        return None
    data = json.loads(raw)
    return _CancelMarker(message_id=data["message_id"], created_at=float(data["created_at"]))


class _CancelSignal:
    """Carries the watcher's supersede verdict to the owner so a self-cancel is told from an external one.

    ``superseded_by`` is the successor message's id once the watcher has cancelled the owner; it
    stays ``None`` when the owner's cancellation came from anywhere else (a lost lease, a shutdown).
    """

    __slots__ = ("superseded_by",)

    def __init__(self) -> None:
        self.superseded_by: str | None = None


@asynccontextmanager
async def cancel_watch(route: ConversationRoute, batch: Batch) -> AsyncIterator[None]:
    """Cancel the turn in favour of a newer message while the body runs — the cancel watcher.

    A task polls the shared per-thread cancel marker every ``overlap_cancel_poll_seconds``. When
    the marker names a message strictly newer than the batch's newest member it sets the signal
    and cancels the owner task; the owner translates that self-cancel into
    :class:`TurnSupersededError`. A cancel from anywhere else (a lost thread lease, a shutdown)
    passes through untouched. Cross-worker by construction: the marker lives in the shared Redis
    and the watcher runs in the holder, so there is no local fast path — one mechanism. A worker
    with no conversations Redis has no marker and the body runs unwatched.
    """
    settings = accessors._store().settings
    if settings.in_memory:
        yield
        return
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("cancel_watch must run inside a task to be able to cancel it")
    signal = _CancelSignal()
    watcher: asyncio.Task[None] | None = None
    try:
        watcher = asyncio.create_task(_watch_cancel(settings, batch, owner, signal))
        try:
            yield
        except asyncio.CancelledError:
            if signal.superseded_by is not None:
                # This turn's own cancel from a newer message — translate it; the task is
                # un-cancelled so the raised error propagates as a normal supersede, while a
                # cancel from anywhere else passes through untouched.
                if hasattr(owner, "uncancel"):
                    owner.uncancel()
                raise TurnSupersededError(signal.superseded_by) from None
            raise
    finally:
        pending_owner_cancel = False
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                # Only the watcher's own cancellation is swallowed here; a cancel delivered to the
                # owner in this window (a real shutdown, or a supersede that raced a completing
                # turn) leaves ``cancelling() > 0`` and is re-raised after cleanup, never absorbed.
                if owner.cancelling() > 0:
                    pending_owner_cancel = True
        if pending_owner_cancel:
            raise asyncio.CancelledError


async def _watch_cancel(
    settings: ConversationsSettings, batch: Batch, owner: asyncio.Task[object], signal: _CancelSignal
) -> None:
    """Poll the thread's cancel marker and cancel ``owner`` when it names a newer message.

    Checks immediately, then every ``overlap_cancel_poll_seconds``. ``newer`` is thread-index
    order — ``(created_at, message_id)`` strictly greater than the batch's newest member, so a
    marker naming a member of the batch (or an older message) never cancels it. A transient read
    error is logged and retried; a silent death would let a superseding message go unheeded.
    """
    key = settings.overlap_cancel_key(batch.lead.thread_id)
    newest = batch.newest
    while True:
        try:
            marker = await _read_cancel_marker(settings, key)
        except Exception:
            logger.error(
                "conversations: reading the overlap-cancel marker for thread %r failed; retrying in %ss",
                batch.lead.thread_id,
                settings.overlap_cancel_poll_seconds,
                exc_info=True,
            )
        else:
            if marker is not None and (marker.created_at, marker.message_id) > newest:
                signal.superseded_by = marker.message_id
                owner.cancel()
                return
        await asyncio.sleep(settings.overlap_cancel_poll_seconds)


__all__ = [
    "Batch",
    "cancel_watch",
    "is_message_turn",
    "resolve_batch",
    "set_cancel_marker",
    "supersede_lead",
    "turn_text",
]
