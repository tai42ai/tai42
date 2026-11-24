"""Make a stored failed turn visible to an operator: the ERROR log and the platform event.

Every door that ends a turn in failure writes its record through one of three writes — a live
turn's transition out of intake, a stranded turn's re-drive, a parked turn's completion mint —
and each calls :func:`announce_failed_turn` once that write has landed, so nothing here names an
outcome that was not stored. The transition and the re-drive are guarded and return before the
call when they lose their guard; the mint dedupes by a read before its write, so two deliveries
racing one completion id can each write and announce.
"""

from __future__ import annotations

import logging

from tai42_skeleton.conversations.models import ConversationRecord

logger = logging.getLogger("tai42_skeleton.conversations.turn")

__all__ = ["TURN_FAILED_EVENT_TOPIC", "announce_failed_turn"]

# The platform-event topic emitted once for every failed conversation turn, message and event
# alike, whether or not the participant is sent a line. Core states the fact; a deployment wires a
# hook on this topic to decide what an operator sees. It rides alongside the record's ``error``
# outcome and the ERROR log, never replacing them.
TURN_FAILED_EVENT_TOPIC = "conversations_turn_failed"


async def announce_failed_turn(record: ConversationRecord) -> None:
    """Log and event ``record`` when its stored outcome is a failed turn; a no-op for any other outcome.

    A failure that sends the participant no line reaches no one through the conversation, so it is
    logged at ERROR with its detail here; a failure that delivers a line was logged where it
    happened. Every failure, either way, emits :data:`TURN_FAILED_EVENT_TOPIC` once.
    """
    if record.answer_status != "error":
        return
    if record.answer is None:
        logger.error(
            "conversations: the %s turn %s on route %r failed and owes the participant no line; nothing is "
            "sent to them and the record carries the error: %s",
            record.inbound_kind,
            record.message_id,
            record.route_name,
            record.error,
        )
    await _emit_turn_failed(record)


async def _emit_turn_failed(record: ConversationRecord) -> None:
    """Emit ``conversations_turn_failed`` for ``record``'s failed turn.

    The payload names the record (``message_id``) and where it sits (thread, route, door, channel,
    participant address), who sent the turn (``inbound_kind``) and the failure detail the record
    keeps in ``error`` — never the inbound content. Best-effort: a hooks-manager failure is logged
    and swallowed so the event never changes the turn's stored outcome or its delivery.
    """
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {
        "message_id": record.message_id,
        "thread_id": record.thread_id,
        "route_name": record.route_name,
        "door": record.door,
        "channel": record.channel,
        "client_address": record.client_address,
        "inbound_kind": record.inbound_kind,
        "error": record.error,
    }
    try:
        await get_hooks_manager().on_event(topic=TURN_FAILED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "conversations: failed to emit %r for the failed turn %s on route %r",
            TURN_FAILED_EVENT_TOPIC,
            record.message_id,
            record.route_name,
            exc_info=True,
        )
