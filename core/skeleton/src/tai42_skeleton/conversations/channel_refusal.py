"""Refusing a reply on the channel door: the admission checks before any chunk is sent, and the notice.

A reply the channel can never take — a part it cannot render or draw, or an answer over the fan-out
cap — is refused before its first chunk: the record fails loudly and terminally. A refusal of a
reachable participant's reply, here or mid-send, tells them the uniform could-not-deliver notice.
"""

from __future__ import annotations

import logging
import time

from tai42_contract.channels import Channel, ChannelNotification
from tai42_contract.conversations import AnswerPart

from tai42_skeleton.channels.capabilities import RICH_CAPABILITIES, check_channel_part, unsupported_capability
from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.sends import notification_part, recorded_send

logger = logging.getLogger(__name__)


async def notify_refusal(store: ConversationRecordStore, record: ConversationRecord, channel: Channel) -> None:
    """Tell the participant the uniform could-not-deliver notice after a reachable channel refused a reply.

    A terminal, non-transport refusal on a reachable channel (an oversized answer, an unrenderable
    part, or a permanent input refusal) has already failed the record; the participant is then sent
    the SAME uniform client-safe text a failed turn the participant sent is answered with, through the
    same channel, resolved via the route's ``reply_texts.error`` when it carries one (a route-lookup
    fault falls back to the built-in text). The notice is a ``notice`` send record of its own,
    bounded by ``delivery_send_timeout_seconds`` and never retried; it never changes the state of
    the failed reply's record. The send is best-effort and loud: a store fault on the notice's own
    record write stops the notice before it is sent, and that or any send failure is logged at
    ERROR here, because the reply's record is already terminally ``failed`` and there is no second
    delivery machine for the notice.

    A reply to a turn the participant sent earns the notice — a client turn and a park-completion
    resumed reply alike (both are answers the participant awaits). A reply to a turn the participant
    did not send is skipped: an operator's hand-injected send (``operator_send``), and the reply to
    an event turn (``inbound_kind`` ``event``) on a client record or on the completion record that
    answers it. Neither answers anything the participant asked and the uniform text speaks of "your
    message", so its refusal fails loudly with the record's ``failed`` state, its ERROR log and the
    admin failed-delivery listing as the signal, exactly as a transport exhaustion's is.
    """
    if record.operator_send or record.inbound_kind == "event":
        return

    from tai42_skeleton.conversations import cache
    from tai42_skeleton.conversations.turn.outcome import _error_answer_text

    try:
        route = await cache.get_conversations_manager().get_route(record.route_name)
    except Exception:
        # The route only enriches the notice text (its ``reply_texts.error``); a manager/store
        # fault on this lookup falls back to the built-in uniform text — logged loudly, never
        # silent. A store fault on the notice's own record write below stops the notice.
        logger.error(
            "conversations: could not resolve route %r to enrich the could-not-deliver notice for record %s; "
            "falling back to the built-in uniform text",
            record.route_name,
            record.message_id,
            exc_info=True,
        )
        route = None
    notification = ChannelNotification(
        message=_error_answer_text(route),
        recipient=record.client_address,
        sender_identity=record.our_identity,
    )
    channel_name = record.channel
    if channel_name is None:
        raise RuntimeError(f"record {record.message_id} names no channel to send its notice on")
    try:
        await recorded_send(
            channel=channel_name,
            channel_obj=channel,
            address=channel.resolve_address(record.client_address, record.our_identity),
            parts=[notification_part(notification)],
            sent_by="notice",
            send=lambda: channel.notify(notification),
            bound=store.settings.delivery_send_timeout_seconds,
        )
    except Exception:
        logger.error(
            "conversations: could not deliver the client-safe could-not-deliver notice for record %s on channel "
            "%r; the record stays failed and the notice is not retried",
            record.message_id,
            record.channel,
            exc_info=True,
        )


def unsupported_rich_capability(channel: Channel, parts: list[AnswerPart]) -> str | None:
    """The name of the FIRST richer-send capability a part needs that ``channel`` lacks, or ``None``.

    ``None`` when every part is renderable. The capability flags are the same
    :data:`RICH_CAPABILITIES` ``notify_user`` guards on — a text-only channel advertises none, so a
    plain-text answer always passes. A part that needs
    an unadvertised capability can never be rendered, so the executor refuses the record loudly
    rather than handing the channel a field it drops.
    """
    for part in parts:
        missing = unsupported_capability(
            channel,
            {field: getattr(part, field) for field, _flag, _label, _word in RICH_CAPABILITIES},
        )
        if missing is not None:
            return missing[0]
    return None


async def admit_channel_send(
    store: ConversationRecordStore,
    record: ConversationRecord,
    channel: Channel,
    parts: list[AnswerPart],
    pending: list[tuple[int, str, bool]],
    part_texts: list[str],
    max_chars: int,
    attempts: int,
    token: str,
) -> bool:
    """The pre-first-chunk admission refusals for a fresh send.

    Returns ``True`` when the send must stop (the record was refused), ``False`` when it may
    proceed. Refuses — before any chunk goes out — a part whose media/template/options the channel
    cannot render, a template or form part the channel's own checks refuse
    (:func:`~tai42_skeleton.channels.capabilities.check_channel_part`: the template hook, the subset
    walk, the form hook), and an answer whose total chunk count is over the fan-out cap. Each
    refusal fails the record and sends the uniform notice. All are ADMISSION decisions, never
    retroactive; a resume (called only when nothing is sent) is never refused here.

    ``pending`` is the send plan the send loop then runs, so a part's check reads the exact final
    chunk its template or schema rides.
    """
    missing = unsupported_rich_capability(channel, parts)
    if missing is not None:
        await _refuse_unrenderable_parts(store, record, channel, missing, attempts, token)
        return True
    final_chunks = {part_index: chunk for part_index, chunk, final in pending if final}
    for part_index, part in enumerate(parts):
        if part.template is None and part.schema is None:
            continue
        try:
            check_channel_part(channel, message=final_chunks[part_index], template=part.template, schema=part.schema)
        except ValueError as exc:
            await _refuse_undrawable_part(store, record, channel, part_index, exc, attempts, token)
            return True
    # The cap counts the chunks of EVERY part (each part is chunked independently — its
    # boundaries are message boundaries), so the whole ordered answer is bounded by one knob.
    from tai42_skeleton.conversations import delivery as _pkg

    answer_chunks = sum(len(_pkg.split_message(text, max_chars)) for text in part_texts)
    if answer_chunks > store.settings.max_outbound_chunks:
        await _refuse_oversized_answer(store, record, channel, answer_chunks, attempts, token)
        return True
    return False


async def _refuse_oversized_answer(
    store: ConversationRecordStore,
    record: ConversationRecord,
    channel: Channel,
    chunk_count: int,
    attempts: int,
    token: str,
) -> None:
    """Refuse an answer past the fan-out cap: fail the record loudly, then notify the participant.

    The whole answer is refused rather than fanned out or truncated. When THIS worker's write
    terminalises the record, the reachable participant is told the uniform could-not-deliver notice
    through the same channel; a foreign takeover owns the record, so this worker sends no notice.
    """
    settings = store.settings
    logger.error(
        "conversations: record %s answer splits into %d chunk(s), over the max_outbound_chunks cap of %d on "
        "channel %r; failing the record and notifying the participant",
        record.message_id,
        chunk_count,
        settings.max_outbound_chunks,
        record.channel,
    )
    failed = await store.mark_failed(record.message_id, attempts, time.time(), token, sends=[])
    if failed == 1:
        await notify_refusal(store, record, channel)


async def _refuse_unrenderable_parts(
    store: ConversationRecordStore,
    record: ConversationRecord,
    channel: Channel,
    missing: str,
    attempts: int,
    token: str,
) -> None:
    """Fail a record whose parts need a richer-send capability the channel does not advertise, then notify.

    Covers a media/template/options/schema part routed to a text-only channel: the record fails
    loudly and terminally so it is never re-driven, and no half-rendered send goes out. The channel
    is reachable but refused this reply's shape, so — when THIS worker's write terminalises the
    record — the participant is told the uniform could-not-deliver notice through the same channel;
    the missing capability itself stays an operator signal in the log.
    """
    logger.error(
        "conversations: record %s carries a part needing %s, which channel %r does not advertise support for; "
        "failing the record (a media/template/options/schema part cannot be routed to a text-only channel)",
        record.message_id,
        missing,
        record.channel,
    )
    failed = await store.mark_failed(record.message_id, attempts, time.time(), token, sends=[])
    if failed == 1:
        await notify_refusal(store, record, channel)


async def _refuse_undrawable_part(
    store: ConversationRecordStore,
    record: ConversationRecord,
    channel: Channel,
    part_index: int,
    refusal: ValueError,
    attempts: int,
    token: str,
) -> None:
    """Fail a record with a template or form part the channel's own checks refuse, then notify.

    The channel advertises the part's capability but cannot draw this part (a form outside the
    channel subset or over the channel's own form limits, a template whose parameters the channel
    cannot map). The record fails loudly and terminally so it is never re-driven, and no part of
    the reply goes out. When THIS worker's write terminalises the record, the participant is told
    the uniform could-not-deliver notice through the same channel; the refusal itself, naming the
    property, parameter or limit, stays an operator signal in the log.
    """
    logger.error(
        "conversations: record %s part %d cannot be drawn on channel %r: %s; failing the record before any "
        "part of the reply is sent",
        record.message_id,
        part_index + 1,
        record.channel,
        refusal,
    )
    failed = await store.mark_failed(record.message_id, attempts, time.time(), token, sends=[])
    if failed == 1:
        await notify_refusal(store, record, channel)


__all__ = ["admit_channel_send", "notify_refusal", "unsupported_rich_capability"]
