"""Channel-door delivery: chunk a produced answer through a channel's ``notify``.

Resumes an interrupted multi-message send from the per-chunk ledger without re-sending what a
provider already accepted, and refuses loudly a part the channel cannot render or an over-cap
fan-out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from tai42_contract.channels import Channel, ChannelDeliveryError, ChannelInputError, ChannelNotification
from tai42_contract.conversations import AnswerPart

from tai42_skeleton.conversations.ledger import ChannelSendLedger, LedgerInconsistentError, SentChunk
from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

logger = logging.getLogger(__name__)

# Client-safe reply when an answer splits into more provider messages than the fan-out cap
# allows; the whole answer is refused rather than fanned out or silently truncated.
_OVERSIZED_ANSWER_TEXT = "Sorry, the answer was too long to send here. Please ask for a shorter response."


def _validate_ledger_parts(parts: list[AnswerPart], sent_by_part: dict[int, int], seen_parts: set[int]) -> None:
    """Raise :class:`LedgerInconsistentError` when the ledger cannot describe ``parts``.

    Inconsistent means a part index past the answer, a part with more characters ledgered than it
    holds, or a gap where an earlier part is not fully sent under a later part that has already
    started (for a media-only earlier part, "fully sent" means it has a ledger entry at all, since
    its char count is zero).
    """
    for part_index, chars in sent_by_part.items():
        if not 0 <= part_index < len(parts):
            raise LedgerInconsistentError(
                f"channel send ledger names part {part_index}, but the answer has {len(parts)} part(s)"
            )
        if chars > len(parts[part_index].message):
            raise LedgerInconsistentError(
                f"channel send ledger claims {chars} character(s) already sent of part {part_index} that is "
                f"{len(parts[part_index].message)} character(s) long"
            )
    if seen_parts:
        highest = max(seen_parts)
        for earlier in range(highest):
            expected = len(parts[earlier].message)
            done = earlier in seen_parts and sent_by_part.get(earlier, 0) == expected
            if not done:
                raise LedgerInconsistentError(
                    f"channel send ledger resumes at part {highest} but part {earlier} is only "
                    f"{sent_by_part.get(earlier, 0)}/{expected} character(s) sent"
                )


def _remaining_parts(parts: list[AnswerPart], sent: list[SentChunk]) -> list[tuple[int, str]]:
    """The still-unsent portion of each answer part, as ``(part_index, remaining_text)`` in order.

    The resume plan the send loop chunks and delivers. A TEXT part yields its unsent
    tail; a MEDIA-ONLY part (blank ``message``) yields one ``(part_index, "")`` entry — a single
    zero-text send carrying the part's media — but ONLY until it has a ledger entry, after which
    it is complete and contributes nothing. A single-part text answer degenerates to
    ``[(0, text[chars_sent:])]``, byte-identical to a plain per-answer resume.

    The ledger is append-only in send order, so its entries are all of part 0's chunks, then
    part 1's, and so on; :func:`_validate_ledger_parts` enforces that invariant.
    """
    sent_by_part: dict[int, int] = {}
    seen_parts: set[int] = set()
    for chunk in sent:
        sent_by_part[chunk.part] = sent_by_part.get(chunk.part, 0) + chunk.chars
        seen_parts.add(chunk.part)
    _validate_ledger_parts(parts, sent_by_part, seen_parts)
    plan: list[tuple[int, str]] = []
    for part_index, part in enumerate(parts):
        text = part.message
        remaining = text[sent_by_part.get(part_index, 0) :]
        if remaining:
            plan.append((part_index, remaining))
        elif not text.strip() and part_index not in seen_parts:
            # A media-only part carries no text but still owes one zero-text send of its media;
            # it is planned only until its ledger entry marks it delivered.
            plan.append((part_index, ""))
    return plan


def _part_notification(part: AnswerPart, chunk: str, record: ConversationRecord, *, final: bool) -> ChannelNotification:
    """The :class:`ChannelNotification` for one chunk of ``part``.

    The part's rich fields (media, location, template, options, sections, header, footer, schema,
    and a form part's per-send data/pages) ride the FINAL chunk of the part — its completed
    message — so a multi-chunk part's earlier chunks are plain text and the media/buttons/form
    land with the last.
    A CONTENT-ONLY part (media- or location-only) has a single final chunk of ``""``: the
    notification then carries a blank message plus that content, which the contract admits exactly
    because the content is present. ``recipient``/``sender_identity`` are the per-delivery routing
    the record carries, never per-part. The mapping is 1:1 — every AnswerPart content/interactive
    field maps to its identically-named ChannelNotification field.
    """
    return ChannelNotification(
        message=chunk,
        recipient=record.client_address,
        sender_identity=record.our_identity,
        media=part.media if final else None,
        location=part.location if final else None,
        template=part.template if final else None,
        options=part.options if final else None,
        sections=part.sections if final else None,
        header=part.header if final else None,
        footer=part.footer if final else None,
        schema=part.schema if final else None,
        data=part.data if final else None,
        pages=part.pages if final else None,
    )


def _unsupported_rich_capability(channel: Channel, parts: list[AnswerPart]) -> str | None:
    """The name of the FIRST richer-send capability a part needs that ``channel`` lacks, or ``None``.

    ``None`` when every part is renderable. The capability flags are the same
    OPTIONAL class attributes ``notify_user`` guards on, read defensively with ``getattr`` —
    a text-only channel advertises none, so a plain-text answer always passes. A part that needs
    an unadvertised capability can never be rendered, so the executor refuses the record loudly
    rather than handing the channel a field it drops.
    """
    for part in parts:
        if part.media is not None and not getattr(channel, "supports_media_notifications", False):
            return "media"
        if part.location is not None and not getattr(channel, "supports_location_notifications", False):
            return "location"
        if part.template is not None and not getattr(channel, "supports_template_notifications", False):
            return "template"
        if part.options is not None and not getattr(channel, "supports_interactive_notifications", False):
            return "interactive options"
        if part.sections is not None and not getattr(channel, "supports_interactive_notifications", False):
            # A sectioned list IS an interactive choice surface, same capability as flat options.
            return "interactive sections"
        if part.schema is not None and not getattr(channel, "supports_form_notifications", False):
            return "form"
        # A header/footer is a pure enhancement of an already-gated interactive message (it rides
        # options/sections), so a channel that renders the choice surface but not the header/footer
        # simply omits them — no capability gate, mirroring a media caption a channel may drop.
    return None


async def _channel_delivery_preconditions(
    store: ConversationRecordStore, record: ConversationRecord, token: str
) -> tuple[Channel, str, int, str]:
    """Gate a channel record and resolve its channel, name, width and answer.

    Fails the record and raises loudly on an impossible state or config error (no channel name, a
    silent outcome on the channel door, no answer, no ``max_message_chars`` entry, or an
    unregistered channel).
    """
    from tai42_skeleton.conversations import delivery as _pkg

    settings = store.settings
    channel_name = record.channel
    if channel_name is None:
        raise RuntimeError(f"channel record {record.message_id!r} carries no channel to deliver on")
    if record.answer_status == "silent":
        # A channel-door silent turn is terminal ``silent`` and never reaches delivery; a
        # ``silent`` answer_status on a channel record is an impossible state, raised loudly.
        raise RuntimeError(
            f"channel record {record.message_id!r} carries a silent outcome; the channel door never delivers one"
        )
    answer = record.answer
    if answer is None:
        raise RuntimeError(
            f"channel record {record.message_id!r} is {record.delivery_status.value} and carries no answer to send"
        )
    max_chars = settings.max_message_chars.get(channel_name)
    if max_chars is None:
        # Config error: fail the record so the outcome is visible, then raise loudly.
        await store.mark_failed(record.message_id, await store.bump_attempt(record.message_id), time.time(), token)
        raise RuntimeError(
            f"channel {channel_name!r} has no max_message_chars entry; add it to CONVERSATIONS_MAX_MESSAGE_CHARS"
        )
    try:
        channel = _pkg.tai42_app.channels.get(channel_name)
    except KeyError as exc:
        # Same config-error treatment: fail the record so the sweep stops re-driving a
        # send that could never complete, then raise loudly.
        await store.mark_failed(record.message_id, await store.bump_attempt(record.message_id), time.time(), token)
        raise RuntimeError(
            f"channel {channel_name!r} is routed but is not registered on this deployment; load its channel "
            "plugin or remove the route"
        ) from exc
    return channel, channel_name, max_chars, answer


async def _admit_channel_send(
    store: ConversationRecordStore,
    record: ConversationRecord,
    channel: Channel,
    parts: list[AnswerPart],
    part_texts: list[str],
    max_chars: int,
    attempts: int,
    token: str,
) -> bool:
    """The pre-first-chunk admission refusals for a fresh send.

    Returns ``True`` when the send must stop (the record was refused), ``False`` when it may
    proceed. Refuses — before any chunk goes out — a part whose media/template/options the channel
    cannot render, and an answer whose total chunk count is over the fan-out cap. Both are
    ADMISSION decisions, never retroactive; a resume (called only when nothing is sent) is never
    refused here.
    """
    missing = _unsupported_rich_capability(channel, parts)
    if missing is not None:
        await _refuse_unrenderable_parts(store, record, missing, attempts, token)
        return True
    # The cap counts the chunks of EVERY part (each part is chunked independently — its
    # boundaries are message boundaries), so the whole ordered answer is bounded by one knob.
    from tai42_skeleton.conversations import delivery as _pkg

    answer_chunks = sum(len(_pkg.split_message(text, max_chars)) for text in part_texts)
    if answer_chunks > store.settings.max_outbound_chunks:
        await _refuse_oversized_answer(store, record, channel, answer_chunks, attempts, token)
        return True
    return False


def _channel_send_plan(
    parts: list[AnswerPart], plan: list[tuple[int, str]], max_chars: int
) -> list[tuple[int, str, bool]]:
    """Flatten the resume ``plan`` to ordered ``(part_index, chunk, final)`` sends.

    Each unsent part portion is chunked at ``max_chars``, the part index rides each entry so a
    resume tells one part's chunks from the next's, and ``final`` marks the chunk carrying the part's
    media/options — the last NON-BLANK chunk of a text part, the single blank chunk of a
    MEDIA-ONLY part, and none of an all-whitespace resume TAIL of a text part (whose content and
    rich fields already went out, so marking any of it final would blank-send or double-send).
    """
    from tai42_skeleton.conversations import delivery as _pkg

    pending: list[tuple[int, str, bool]] = []
    for part_index, remaining in plan:
        remaining_chunks = _pkg.split_message(remaining, max_chars)
        non_blank = [i for i, chunk in enumerate(remaining_chunks) if chunk.strip()]
        if non_blank:
            rich_at: int | None = non_blank[-1]
        elif not parts[part_index].message.strip():
            # A genuine MEDIA-ONLY part (blank ``message``): its single ``""`` chunk IS the
            # deliverable and carries the media, so it is final.
            rich_at = len(remaining_chunks) - 1
        else:
            # An all-whitespace tail of a text part whose content already went out: no chunk
            # here is final; every remaining whitespace chunk stays ledger-skip.
            rich_at = None
        for offset, chunk in enumerate(remaining_chunks):
            pending.append((part_index, chunk, offset == rich_at))
    return pending


async def _reindex_resume(
    store: ConversationRecordStore, record: ConversationRecord, channel_name: str, sent: list[SentChunk], answer: str
) -> list[str]:
    """The ids already accepted before a resume, in send order.

    On a resume they are re-indexed first — a chunk accepted just before a crash may never have
    reached the reverse index, and a receipt naming an unindexed id resolves to nothing. Empty on
    a fresh send.
    """
    outbound_ids = [outbound_id for chunk in sent for outbound_id in chunk.outbound_ids]
    if sent:
        await store.index_outbound(channel_name, outbound_ids, record.message_id)
        chars_sent = sum(chunk.chars for chunk in sent)
        logger.info(
            "conversations: resuming channel delivery of record %s on %r at character %d/%d (%d chunk(s) already "
            "accepted by the provider)",
            record.message_id,
            channel_name,
            chars_sent,
            len(answer),
            len(sent),
        )
    return outbound_ids


async def _fail_partial_send(
    store: ConversationRecordStore,
    ledger: ChannelSendLedger,
    record: ConversationRecord,
    channel_name: str,
    accepted_chunks: int,
    total_chunks: int,
    attempts: int,
    token: str,
    *,
    input_refusal: bool,
) -> None:
    """Mark a mid-sequence send terminal ``failed`` and clear the ledger under this worker's write.

    The medium offers no idempotency key, so a retry would re-send accepted chunks. The ledger is
    cleared ONLY under this worker's own terminal write (a foreign takeover owns the ledger it
    resumes from). Logs a delivery refusal or a permanent input-shape refusal.
    """
    failed = await store.mark_failed(record.message_id, attempts, time.time(), token)
    if failed == 1:
        await ledger.clear(record.message_id)
    if input_refusal:
        logger.error(
            "conversations: channel %r permanently refused the input for record %s after %d/%d chunk(s) "
            "(failed write returned %d)",
            channel_name,
            record.message_id,
            accepted_chunks,
            total_chunks,
            failed,
            exc_info=True,
        )
    else:
        logger.error(
            "conversations: channel delivery of record %s on %r failed after %d/%d chunk(s) (failed write returned %d)",
            record.message_id,
            channel_name,
            accepted_chunks,
            total_chunks,
            failed,
            exc_info=True,
        )


async def _run_channel_send_loop(
    store: ConversationRecordStore,
    ledger: ChannelSendLedger,
    record: ConversationRecord,
    channel: Channel,
    channel_name: str,
    pending: list[tuple[int, str, bool]],
    sent: list[SentChunk],
    outbound_ids: list[str],
    parts: list[AnswerPart],
    attempts: int,
    token: str,
    settings: ConversationsSettings,
) -> bool:
    """Execute the pending sends under a lease refreshed before each one, appending accepted ids in place.

    Accepted ids are appended to ``outbound_ids``. Returns ``True`` when every chunk went out,
    ``False`` when the send stopped early: the lease was lost, the provider did not answer within
    the send timeout (the chunk is left unledgered so a re-drive re-sends it), or a delivery/input
    refusal made the record terminal ``failed``.
    """
    total_chunks = len(sent) + len(pending)
    accepted_chunks = len(sent)
    try:
        for part_index, chunk, final in pending:
            # Refresh the lease BEFORE each send, and bound the send strictly under it, so the
            # whole notify window is covered by a claim this worker holds.
            held = await store.claim_delivery(
                record.message_id, time.time(), token, settings.delivery_claim_lease_seconds
            )
            if held != 1:
                logger.warning(
                    "conversations: lost the delivery lease on record %s after %d/%d chunk(s) (claim returned %d); "
                    "leaving the remainder to the worker that holds it now",
                    record.message_id,
                    accepted_chunks,
                    total_chunks,
                    held,
                )
                return False
            if not chunk.strip() and not final:
                # A whitespace-only, non-final chunk carries neither text nor rich fields: it is
                # ledgered so the resume arithmetic stays exact, and no provider call is made.
                await ledger.append(record.message_id, len(chunk), [], part=part_index)
                accepted_chunks += 1
                continue
            try:
                async with asyncio.timeout(settings.delivery_send_timeout_seconds):
                    ids = await channel.notify(_part_notification(parts[part_index], chunk, record, final=final))
            except TimeoutError:
                # Indeterminate: the provider may have taken the chunk. It is deliberately NOT
                # ledgered, so a re-drive re-sends it — a duplicate is the cheaper side of a loss.
                logger.exception(
                    "conversations: channel %r did not answer within %ss for chunk %d/%d of record %s; the chunk is "
                    "indeterminate and is left unledgered for a re-drive to re-send",
                    channel_name,
                    settings.delivery_send_timeout_seconds,
                    accepted_chunks + 1,
                    total_chunks,
                    record.message_id,
                )
                return False
            # ``None`` from a channel off the id-returning contract is an accepted send with no
            # correlatable id, not a dropped one. Ledger BEFORE the reverse index: a crash between
            # the two costs an unresolvable receipt, a crash before the ledger costs a duplicate.
            accepted = list(ids or [])
            await ledger.append(record.message_id, len(chunk), accepted, part=part_index)
            await store.index_outbound(channel_name, accepted, record.message_id)
            outbound_ids.extend(accepted)
            accepted_chunks += 1
    except ChannelDeliveryError:
        await _fail_partial_send(
            store, ledger, record, channel_name, accepted_chunks, total_chunks, attempts, token, input_refusal=False
        )
        return False
    except ChannelInputError:
        await _fail_partial_send(
            store, ledger, record, channel_name, accepted_chunks, total_chunks, attempts, token, input_refusal=True
        )
        return False
    return True


async def _deliver_channel(store: ConversationRecordStore, record: ConversationRecord, token: str) -> None:
    """Drive one channel record's send to provisional, resuming from the ledger.

    Never re-sends an accepted chunk. Orchestrates: preconditions → ledger read + resume plan →
    fresh-send admission → send plan → send loop → mark provisional + fallback confirmation.
    """
    from tai42_skeleton.conversations import delivery as _pkg

    settings = store.settings
    channel, channel_name, max_chars, answer = await _channel_delivery_preconditions(store, record, token)

    # A single plain-text answer is one text-only part; a richer or multi-message answer delivers
    # each part as its own message, in order. The send loop is IDENTICAL either way, so the
    # single-part path stays byte-exact with a plain per-answer send.
    parts = record.answer_parts or [AnswerPart(message=answer)]
    part_texts = [part.message for part in parts]
    ledger = ChannelSendLedger(settings)
    # Account the attempt BEFORE the first fallible step, so every fault below is bounded by
    # ``delivery_max_attempts`` instead of leaving the record pending_delivery forever.
    attempts = await store.bump_attempt(record.message_id)
    try:
        sent = await ledger.sent_chunks(record.message_id)
        plan = _remaining_parts(parts, sent)
    except LedgerInconsistentError:
        # A ledger that cannot describe the answer can never resume, so the record is failed here
        # before the refusal is raised. A transient store fault instead propagates, leaving the
        # record for the sweep to re-drive.
        failed = await store.mark_failed(record.message_id, attempts, time.time(), token)
        if failed == 1:
            await ledger.clear(record.message_id)
        raise

    if not sent and await _admit_channel_send(store, record, channel, parts, part_texts, max_chars, attempts, token):
        return

    outbound_ids = await _reindex_resume(store, record, channel_name, sent, answer)
    pending = _channel_send_plan(parts, plan, max_chars)
    completed = await _run_channel_send_loop(
        store, ledger, record, channel, channel_name, pending, sent, outbound_ids, parts, attempts, token, settings
    )
    if not completed:
        return

    outcome = await store.mark_provisional(record.message_id, outbound_ids, attempts, time.time(), token)
    if outcome != 1:
        # The record left this worker's hands; its ledger belongs to whoever holds it now.
        logger.warning(
            "conversations: record %s was not moved to provisional after a full send (provisional write returned "
            "%d); the send ledger is left for the worker that owns it now",
            record.message_id,
            outcome,
        )
        return
    await ledger.clear(record.message_id)
    # Fallback confirmation for a medium whose receipt never arrives.
    _pkg._spawn(_pkg._confirm_after_grace(record.message_id, settings.delivery_grace_seconds))


async def _refuse_oversized_answer(
    store: ConversationRecordStore,
    record: ConversationRecord,
    channel: Channel,
    chunk_count: int,
    attempts: int,
    token: str,
) -> None:
    """Refuse an answer past the fan-out cap: send ONE client-safe reply and fail the record loudly.

    A best-effort provider refusal is suppressed — the record still fails.
    """
    settings = store.settings
    logger.error(
        "conversations: record %s answer splits into %d chunk(s), over the max_outbound_chunks cap of %d on "
        "channel %r; refusing with a client-safe reply and failing the record",
        record.message_id,
        chunk_count,
        settings.max_outbound_chunks,
        record.channel,
    )
    with contextlib.suppress(ChannelDeliveryError, TimeoutError):
        async with asyncio.timeout(settings.delivery_send_timeout_seconds):
            await channel.notify(
                ChannelNotification(
                    message=_OVERSIZED_ANSWER_TEXT,
                    recipient=record.client_address,
                    sender_identity=record.our_identity,
                )
            )
    await store.mark_failed(record.message_id, attempts, time.time(), token)


async def _refuse_unrenderable_parts(
    store: ConversationRecordStore, record: ConversationRecord, missing: str, attempts: int, token: str
) -> None:
    """Fail a record whose parts need a richer-send capability the channel does not advertise.

    Covers a media/template/options/schema part routed to a text-only channel: the record fails
    loudly and terminally so it is never re-driven, and no half-rendered send goes out. No
    client-safe reply is sent — the missing capability is an operator's business, not a
    participant-facing size hint.
    """
    logger.error(
        "conversations: record %s carries a part needing %s, which channel %r does not advertise support for; "
        "failing the record (a media/template/options/schema part cannot be routed to a text-only channel)",
        record.message_id,
        missing,
        record.channel,
    )
    await store.mark_failed(record.message_id, attempts, time.time(), token)
