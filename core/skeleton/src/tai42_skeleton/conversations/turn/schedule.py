"""Schedule a turn as a background task under the caps, and the intake lease heartbeat.

The scheduled task holds the intake lease for its duration (so a queued turn reads as live),
runs the turn under the per-thread FIFO, and on completion either spawns delivery or resolves
a record its turn task left stranded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from tai42_contract.conversations import ConversationRoute, TurnSupersededError
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.conversations.caps import TurnCaps
from tai42_skeleton.conversations.delivery import spawn_delivery
from tai42_skeleton.conversations.models import OVERLAP_DELIVERY_STATUSES, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn import accessors, overlap, redrive, target
from tai42_skeleton.conversations.turn.routing import _Multichannel

logger = logging.getLogger("tai42_skeleton.conversations.turn")

# Strong references to in-flight turn tasks so one is not GC'd before it persists.
_TURN_TASKS: set[asyncio.Task] = set()


async def _schedule_turn(
    caps: TurnCaps,
    *,
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    intake_token: str,
    deliver_on_completion: bool,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> asyncio.Task[ConversationRecord]:
    """Schedule ``intake``'s turn as a background task consuming the caller's reservation.

    Returns the task whose result is the completed :class:`ConversationRecord`.

    The seam EVERY door schedules a turn through, so the overlap-cancel marker is set here, at
    accept time (never inside the queued task, which a running turn's watcher would never see):
    on a ``running="cancel"`` route, every participant message accept refreshes the thread's
    marker so a turn already in flight learns a newer message is waiting. Set unconditionally —
    the watcher decides relevance.

    The intake lease is refreshed OUTSIDE the caps, so a turn queued behind the FIFO reads
    as live too. ``caps`` MUST be the instance the caller reserved on, or the reservation is
    released on a different instance and the slot leaks.
    """
    if route.overlap.running == "cancel" and overlap.is_message_turn(intake):
        await overlap.set_cancel_marker(intake)

    async def _run() -> ConversationRecord:
        async with _intake_lease_held(intake.message_id, intake_token), caps.run_reserved(intake.thread_id):
            batch = await overlap.resolve_batch(route, intake)
            if batch is None:
                # An earlier decision merged or superseded this record; no turn runs and its
                # already-written outcome (a channel terminal, or an API marker to deliver) stands.
                return await _settled_record(intake)

            async def _target() -> ConversationRecord:
                return await target._complete_turn(
                    route=route,
                    intake=intake,
                    text=overlap.turn_text(route, batch, text),
                    batch=batch,
                    multichannel=multichannel,
                    params=params,
                    form=form,
                    attachments=attachments,
                    location=location,
                )

            try:
                if route.overlap.running == "cancel" and overlap.is_message_turn(intake):
                    async with overlap.cancel_watch(route, batch):
                        return await _target()
                return await _target()
            except TurnSupersededError as exc:
                # The turn yielded (a cooperative tool, or the cancel watcher): resolve the lead
                # ``superseded`` in favour of the newer message and deliver nothing.
                return await overlap.supersede_lead(batch.lead, exc.successor_id)

    task = asyncio.create_task(_run())
    _TURN_TASKS.add(task)
    if deliver_on_completion:
        task.add_done_callback(lambda t: _spawn_delivery_on_success(t, intake.message_id))
    else:
        task.add_done_callback(_TURN_TASKS.discard)
    return task


async def _settled_record(intake: ConversationRecord) -> ConversationRecord:
    """The current record for a lead whose turn did not run — re-read after an earlier decision.

    A merged/superseded follower has left ``accepted``; its record (a channel terminal, or an
    API-door ``pending_delivery`` marker) is returned so the completion callback delivers or
    skips it by its own delivery status. A record gone entirely is logged loudly.
    """
    settled = await accessors._store().get_record(intake.message_id)
    if settled is None:
        logger.warning(
            "conversations: overlap lead %s left intake and its record is gone; no turn ran and nothing is delivered",
            intake.message_id,
        )
        return intake
    return settled


@contextlib.asynccontextmanager
async def _intake_lease_held(message_id: str, token: str) -> AsyncIterator[None]:
    """Refresh ``message_id``'s intake lease for the body's duration.

    So the intake re-drive reads the turn as LIVE and leaves the record to this worker — whether it
    is running or still queued behind the caps.
    """
    refresher = asyncio.create_task(_refresh_intake_lease(message_id, token))
    try:
        yield
    finally:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher


async def _refresh_intake_lease(message_id: str, token: str) -> None:
    """Re-take the intake lease every ``intake_claim_refresh_seconds`` until it is released or lost.

    Runs until the record leaves intake or the lease is lost. A refresh that fails is logged and
    retried — a heartbeat that died quietly would let a live turn be reaped as stranded.
    """
    store = accessors._store()
    settings = store.settings
    while True:
        await asyncio.sleep(settings.intake_claim_refresh_seconds)
        try:
            held = await store.claim_intake(message_id, time.time(), token, settings.intake_claim_lease_seconds)
        except Exception:
            logger.error(
                "conversations: refreshing the intake lease on record %s failed; retrying in %ss",
                message_id,
                settings.intake_claim_refresh_seconds,
                exc_info=True,
            )
            continue
        if held != 1:
            logger.warning(
                "conversations: record %s no longer holds this worker's intake lease (claim returned %d); its "
                "outcome is another worker's to write",
                message_id,
                held,
            )
            return


def _spawn_delivery_on_success(task: asyncio.Task[ConversationRecord], message_id: str) -> None:
    _TURN_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "conversations: turn task for record %s failed before it wrote an outcome; the record is given an "
            "error outcome and the turn is not re-run",
            message_id,
            exc_info=exc,
        )
        _spawn_intake_resolution(message_id)
        return
    status = task.result().delivery_status
    if status is DeliveryStatus.SILENT or status in OVERLAP_DELIVERY_STATUSES:
        # A channel-door silent/merged/superseded turn is terminal already; nothing is ever
        # delivered. The api-door equivalents sit at pending_delivery and are delivered like an
        # answer (a silent or overlap marker), so they fall through to the spawn below.
        return
    spawn_delivery(message_id)


def _spawn_intake_resolution(message_id: str) -> None:
    """Resolve a record this worker left at intake, in this worker, now.

    This worker owns it, and waiting out its intake lease would hold the message unanswered for that
    long.
    """
    task = asyncio.create_task(redrive._resolve_stranded_intake(message_id))
    _TURN_TASKS.add(task)
    task.add_done_callback(_on_intake_resolution_done)


def _on_intake_resolution_done(task: asyncio.Task[None]) -> None:
    _TURN_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("conversations: resolving a record left at intake by a failed turn task failed", exc_info=exc)
