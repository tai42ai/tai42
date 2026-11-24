"""Resolve / re-drive a record left in ``accepted`` mid-turn.

An interrupted commit or a worker that died leaves a record at intake; the in-process
watcher and the periodic re-drive both arbitrate such a record against its inbound claim
and give it the error outcome its turn never produced.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.delivery import spawn_delivery
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.turn.failed_turn import announce_failed_turn
from tai42_skeleton.conversations.turn.outcome import _FailedOutcome
from tai42_skeleton.conversations.turn.record import _outcome_record

logger = logging.getLogger("tai42_skeleton.conversations.turn")


async def _resolve_stranded_intake(message_id: str) -> None:
    """Resolve a record this worker left at intake — an interrupted commit or a turn task that died.

    A record that has already left intake keeps the outcome it carries; one still at intake is arbitrated
    against its inbound pair.
    """
    store = cache.get_conversations_manager().records
    record = await store.get_record(message_id)
    if record is None:
        logger.warning(
            "conversations: record %s is gone, so the turn that failed on it leaves nothing to resolve", message_id
        )
        return
    if record.delivery_status is not DeliveryStatus.ACCEPTED:
        logger.info(
            "conversations: record %s already carries a %s outcome; the turn task's failure resolves nothing",
            message_id,
            record.delivery_status.value,
        )
        return
    await _arbitrate_stranded_intake(store, record)


async def _arbitrate_stranded_intake(store: ConversationRecordStore, record: ConversationRecord) -> None:
    """Resolve one record left at intake against its inbound claim (channel pair or event id).

    The record the claim is committed to takes the error outcome and delivers it; one that lost the claim
    to another attempt owns nothing and is discarded.
    """
    if await _owns_inbound_claim(store, record):
        await _fail_stranded_turn(store, record)
        return
    await store.delete_record(record)
    if record.inbound_kind == "event":
        event = record.inbound_event or {}
        logger.warning(
            "conversations: intake event record %s lost the event claim for %r on route %r to another attempt and "
            "was discarded",
            record.message_id,
            event.get("event_id"),
            record.route_name,
        )
        return
    logger.warning(
        "conversations: intake record %s lost the inbound claim for %r on channel %r to another attempt and was "
        "discarded",
        record.message_id,
        record.provider_message_id,
        record.channel,
    )


async def redrive_accepted() -> None:
    """Resolve every record left in ``accepted`` by a worker that DIED mid-turn.

    The intake lease is the liveness test and it is taken FIRST: a record whose lease is
    still live belongs to a turn running on a sibling worker and is left untouched, so the
    sweep never reaps an in-flight turn. Only a record whose lease has LAPSED is adopted,
    then arbitrated against the inbound claim (a get-or-set): one the claim names someone
    else for is discarded. An adopted record takes the ``error`` outcome and its turn is
    never re-run — a turn dispatches authorized tools, so it is not idempotent.
    """
    store = cache.get_conversations_manager().records
    token = uuid4().hex
    for record in (await store.list_by_status(frozenset({DeliveryStatus.ACCEPTED}))).items:
        try:
            adopted = await store.claim_intake(
                record.message_id, time.time(), token, store.settings.intake_claim_lease_seconds
            )
            if adopted != 1:
                logger.info(
                    "conversations: intake record %s was not adopted by the re-drive (claim returned %d); its turn "
                    "is live on another worker, or its outcome has already landed",
                    record.message_id,
                    adopted,
                )
                continue
            await _arbitrate_stranded_intake(store, record)
        except Exception:
            # One failing record must not abandon every other stranded record in the pass;
            # the next sweep re-drives this one.
            logger.error(
                "conversations: re-driving stranded intake record %s failed; skipped this pass",
                record.message_id,
                exc_info=True,
            )
            continue


async def _owns_inbound_claim(store: ConversationRecordStore, record: ConversationRecord) -> bool:
    """Whether ``record`` is the one its inbound claim is committed to — claim-family-aware.

    An event record arbitrates against the event dedupe family
    (``get_event_owner``/``claim_event`` on ``(route, event_id)``); a channel message record against the
    channel pair; an api-door message record has no provider id to dedupe on and is its own authority.
    """
    if record.inbound_kind == "event":
        event = record.inbound_event or {}
        owner = await store.claim_event(record.route_name, event["event_id"], record.message_id)
        return owner == record.message_id
    if record.channel is None or record.provider_message_id is None:
        return True
    owner = await store.claim_inbound(record.channel, record.provider_message_id, record.message_id)
    return owner == record.message_id


async def _fail_stranded_turn(store: ConversationRecordStore, record: ConversationRecord) -> None:
    """Give an intake record the error outcome its interrupted turn never produced, and deliver its line.

    The one resolution both the in-process watcher and the periodic re-drive apply; the turn is
    never re-run. Losing the guarded transition leaves the existing outcome standing.

    The outcome goes through the same seam a live failed turn takes (:func:`_outcome_record`), so
    a turn the participant sent delivers the route's ``reply_texts.error`` (else the built-in line)
    and an event turn delivers nothing, its failure kept on the record and in the ERROR log. Either
    way the stored failure is announced (:func:`announce_failed_turn`), and a re-drive that lost the
    guard announces nothing. The
    route is looked up best-effort: ANY failure (manager unavailable, route gone, exception)
    resolves the line without it, so this repair path is never less robust than the built-in
    default — the lookup only ever upgrades the text, never blocks the outcome.
    """
    try:
        route = await cache.get_conversations_manager().get_route(record.route_name)
    except Exception:
        route = None
    completed = _outcome_record(
        record, _FailedOutcome(error="turn was interrupted before it produced an answer", reply=None), route
    )
    outcome = await store.complete_record(completed)
    if outcome != 1:
        logger.warning(
            "conversations: intake record %s left intake while it was being re-driven (complete_record answered %d); "
            "its outcome stands as written",
            record.message_id,
            outcome,
        )
        return
    await announce_failed_turn(completed)
    logger.error(
        "conversations: %s record %s on route %r was stranded mid-turn; the turn is NOT re-run and it takes "
        "an error outcome instead",
        record.inbound_kind,
        record.message_id,
        record.route_name,
    )
    if completed.delivery_status is not DeliveryStatus.SILENT:
        spawn_delivery(record.message_id)
