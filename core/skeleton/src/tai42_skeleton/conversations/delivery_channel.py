"""Channel-door delivery: chunk a produced answer through a channel's ``notify``.

Resumes an interrupted multi-message send from the per-chunk ledger without re-sending what a
provider already accepted, and refuses loudly a part the channel cannot render or draw, or an
over-cap fan-out.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tai42_contract.channels import Channel, ChannelDeliveryError, ChannelInputError, ChannelNotification
from tai42_contract.conversations import AnswerPart

from tai42_skeleton.channels.capabilities import RICH_CAPABILITIES, check_channel_part, unsupported_capability
from tai42_skeleton.conversations.ledger import ChannelSendLedger, LedgerInconsistentError, SentChunk
from tai42_skeleton.conversations.models import ConversationRecord, SentMessage
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.sends import notification_part, recorded_send
from tai42_skeleton.conversations.settings import ConversationsSettings

logger = logging.getLogger(__name__)


def _sent_messages(sent: list[SentChunk]) -> list[SentMessage]:
    """The ledger's accepted chunks as the record keeps them: each part with its ids, in send order."""
    return [SentMessage(part=chunk.part, chars=chunk.chars, provider_message_ids=chunk.outbound_ids) for chunk in sent]


def _ref_identity(record: ConversationRecord) -> str:
    """The identity a channel record's message refs are keyed by; a channel record without one is impossible."""
    if record.our_identity is None:
        raise RuntimeError(f"channel record {record.message_id!r} carries no our_identity to index its sent ids by")
    return record.our_identity


async def _notify_refusal(store: ConversationRecordStore, record: ConversationRecord, channel: Channel) -> None:
    """Tell the participant the uniform could-not-deliver notice after a reachable channel refused a reply.

    A terminal, non-transport refusal on a reachable channel (an oversized answer, an unrenderable
    part, or a permanent input refusal) has already failed the record; the participant is then sent
    the SAME uniform client-safe notice the failed park-completion path delivers, through the same
    channel, resolved via the route's ``reply_texts.error`` when it carries one (a route-lookup
    fault falls back to the built-in text). The notice is a ``notice`` send record of its own,
    bounded by ``delivery_send_timeout_seconds`` and never retried; it never changes the state of
    the failed reply's record. The send is best-effort and loud: a store fault on the notice's own
    record write stops the notice before it is sent, and that or any send failure is logged at
    ERROR here, because the reply's record is already terminally ``failed`` and there is no second
    delivery machine for the notice.

    A reply to a participant's turn earns the notice — a client turn and a park-completion resumed
    reply alike (both are answers the participant awaits). Only an operator's hand-injected send
    (``operator_send``) is skipped: it answers nothing the participant asked and the uniform text
    speaks of "your message", so its refusal fails loudly with the admin failed-delivery listing as
    the signal, exactly as a transport exhaustion's is.
    """
    if record.operator_send:
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
    a form part's per-send data/pages and form_tag, and the per-send ``wording`` catalogue) ride
    the FINAL chunk of the part — its completed message — so a multi-chunk part's earlier chunks are
    plain text and the media/words/form land with the last.
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
        form_tag=part.form_tag if final else None,
        wording=part.wording if final else None,
    )


def _unsupported_rich_capability(channel: Channel, parts: list[AnswerPart]) -> str | None:
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


async def _channel_delivery_preconditions(
    store: ConversationRecordStore, record: ConversationRecord, token: str
) -> tuple[Channel, str, int, str]:
    """Gate a channel record and resolve its channel, name, width and answer.

    Fails the record and raises loudly on an impossible state or config error (no channel name, a
    silent outcome on the channel door, no answer, an unregistered channel, or a channel with no
    resolvable split cap).
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
    try:
        channel = _pkg.tai42_app.channels.get(channel_name)
    except KeyError as exc:
        # Config error: fail the record so the sweep stops re-driving a send that could never
        # complete, then raise loudly.
        await store.mark_failed(
            record.message_id, await store.bump_attempt(record.message_id), time.time(), token, sends=[]
        )
        raise RuntimeError(
            f"channel {channel_name!r} is routed but is not registered on this deployment; load its channel "
            "plugin or remove the route"
        ) from exc
    # Split width: the operator override wins, else the channel's own declared ``max_message_chars``
    # cap (read off the channel, the generic per-channel seam). A channel that declares neither is a
    # loud config error — never a silent unbounded or truncated send.
    max_chars = settings.max_message_chars.get(channel_name)
    if max_chars is None:
        max_chars = getattr(channel, "max_message_chars", None)
    if max_chars is None:
        await store.mark_failed(
            record.message_id, await store.bump_attempt(record.message_id), time.time(), token, sends=[]
        )
        raise RuntimeError(
            f"channel {channel_name!r} declares no max_message_chars cap and none is configured; declare the "
            "channel's cap or set CONVERSATIONS_MAX_MESSAGE_CHARS"
        )
    return channel, channel_name, max_chars, answer


async def _admit_channel_send(
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
    missing = _unsupported_rich_capability(channel, parts)
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
) -> list[SentMessage]:
    """The parts already accepted before a resume, in send order, with their ids.

    On a resume their ids are re-indexed first — a chunk accepted just before a crash may never
    have reached the reverse index, and a receipt naming an unindexed id resolves to nothing. The
    re-index is idempotent, so a chunk already indexed is unchanged. Empty on a fresh send.
    """
    if sent:
        outbound_ids = [outbound_id for chunk in sent for outbound_id in chunk.outbound_ids]
        indexed = await store.index_outbound(
            channel_name, _ref_identity(record), record.client_address, outbound_ids, record.message_id
        )
        if indexed == -1:
            logger.warning(
                "conversations: record %s is gone, its thread was forgotten; the ids already sent are not indexed: %s",
                record.message_id,
                outbound_ids,
            )
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
    return _sent_messages(sent)


async def _fail_partial_send(
    store: ConversationRecordStore,
    ledger: ChannelSendLedger,
    record: ConversationRecord,
    channel: Channel,
    channel_name: str,
    accepted_chunks: int,
    total_chunks: int,
    attempts: int,
    token: str,
    sends: list[SentMessage],
    *,
    input_refusal: bool,
    notify: bool,
) -> None:
    """Mark a mid-sequence send terminal ``failed`` at once and clear the ledger under this worker's write.

    The medium offers no idempotency key, so a retry would re-send accepted chunks. The record is
    failed on the FIRST refusal (a non-retryable fault never spends the ``delivery_max_attempts``
    budget), and the ledger is cleared ONLY under this worker's own terminal write (a foreign
    takeover owns the ledger it resumes from). ``input_refusal`` selects the log wording (a
    permanent input-shape refusal vs a delivery failure); ``notify`` — set for a permanent input
    refusal and a NON-retryable delivery refusal, both the channel rejecting a reachable
    participant's reply — sends them the uniform could-not-deliver notice, but only when THIS
    worker's write terminalised the record (a foreign takeover owns it and delivers it itself).
    A retryable (transport) delivery failure fails the record without a notice: the participant
    could not be reached, so the admin failed-delivery listing is the signal. The record keeps the
    ``sends`` of the chunks already accepted.
    """
    failed = await store.mark_failed(record.message_id, attempts, time.time(), token, sends=sends)
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
    if notify and failed == 1:
        await _notify_refusal(store, record, channel)


async def _run_channel_send_loop(
    store: ConversationRecordStore,
    ledger: ChannelSendLedger,
    record: ConversationRecord,
    channel: Channel,
    channel_name: str,
    pending: list[tuple[int, str, bool]],
    sent: list[SentChunk],
    sends: list[SentMessage],
    parts: list[AnswerPart],
    attempts: int,
    token: str,
    settings: ConversationsSettings,
) -> bool:
    """Execute the pending sends under a lease refreshed before each one, appending accepted parts in place.

    Each accepted chunk is appended to ``sends`` with its ids, as the ledger holds it. Returns
    ``True`` when every chunk went out,
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
                sends.append(SentMessage(part=part_index, chars=len(chunk), provider_message_ids=[]))
                accepted_chunks += 1
                continue
            try:
                async with asyncio.timeout(settings.delivery_send_timeout_seconds):
                    sent_outcome = await channel.notify(
                        _part_notification(parts[part_index], chunk, record, final=final)
                    )
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
            # Ledger BEFORE the reverse index: a crash between the two costs an unresolvable
            # receipt, a crash before the ledger costs a duplicate.
            accepted = list(sent_outcome.message_ids)
            await ledger.append(record.message_id, len(chunk), accepted, part=part_index)
            indexed = await store.index_outbound(
                channel_name, _ref_identity(record), record.client_address, accepted, record.message_id
            )
            if indexed == -1:
                logger.warning(
                    "conversations: record %s is gone, its thread was forgotten; the ids of this chunk are not "
                    "indexed: %s",
                    record.message_id,
                    accepted,
                )
            sends.append(SentMessage(part=part_index, chars=len(chunk), provider_message_ids=accepted))
            accepted_chunks += 1
    except ChannelDeliveryError as exc:
        # A NON-retryable delivery refusal (a vendor validation rejection, a hard 4xx, a bad
        # recipient) is the channel refusing a reachable participant's reply, so it earns the
        # notice; a retryable one is transport and does not.
        await _fail_partial_send(
            store,
            ledger,
            record,
            channel,
            channel_name,
            accepted_chunks,
            total_chunks,
            attempts,
            token,
            sends,
            input_refusal=False,
            notify=not exc.retryable,
        )
        return False
    except ChannelInputError:
        await _fail_partial_send(
            store,
            ledger,
            record,
            channel,
            channel_name,
            accepted_chunks,
            total_chunks,
            attempts,
            token,
            sends,
            input_refusal=True,
            notify=True,
        )
        return False
    return True


async def _deliver_channel(store: ConversationRecordStore, record: ConversationRecord, token: str) -> None:
    """Drive one channel record's send to provisional, resuming from the ledger.

    Never re-sends an accepted chunk. Orchestrates: preconditions → ledger read + resume plan →
    fresh-send admission → send plan → send loop → mark provisional + fallback confirmation.
    """
    from tai42_skeleton.conversations import cache
    from tai42_skeleton.conversations import delivery as _pkg
    from tai42_skeleton.conversations.turn import working_signal

    # The turn's answer is about to send, so stop refreshing its working-on-it indicator. A
    # cross-process delivery finds no task in its own registry and relies on the loop's own
    # per-tick status read to stop instead.
    working_signal.stop(record.message_id)

    settings = store.settings
    channel, channel_name, max_chars, answer = await _channel_delivery_preconditions(store, record, token)

    # A single plain-text answer is one text-only part; a richer or multi-message answer delivers
    # each part as its own message, in order. The send loop is IDENTICAL either way, so the
    # single-part path stays byte-exact with a plain per-answer send.
    parts = record.answer_parts or [AnswerPart(message=answer)]
    part_texts = [part.message for part in parts]
    ledger = cache.get_conversations_manager().send_ledger
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
        failed = await store.mark_failed(record.message_id, attempts, time.time(), token, sends=[])
        if failed == 1:
            await ledger.clear(record.message_id)
        raise

    # One send plan, built before admission: admission checks the very chunks the loop sends.
    pending = _channel_send_plan(parts, plan, max_chars)
    if not sent and await _admit_channel_send(
        store, record, channel, parts, pending, part_texts, max_chars, attempts, token
    ):
        return

    sends = await _reindex_resume(store, record, channel_name, sent, answer)
    completed = await _run_channel_send_loop(
        store, ledger, record, channel, channel_name, pending, sent, sends, parts, attempts, token, settings
    )
    if not completed:
        return

    outcome = await store.mark_provisional(record.message_id, sends, attempts, time.time(), token)
    if outcome == 1:
        await ledger.clear(record.message_id)
        # Fallback confirmation for a medium whose receipt never arrives.
        _pkg._spawn(_pkg._confirm_after_grace(record.message_id, settings.delivery_grace_seconds))
        return
    if outcome == 2:
        # A receipt parked during the send terminalised the record delivered in the same step; no
        # grace fallback is needed, the outcome is already recorded.
        await ledger.clear(record.message_id)
        logger.info("conversations: record %s delivered via a receipt parked during send", record.message_id)
        return
    if outcome == 3:
        await ledger.clear(record.message_id)
        logger.error(
            "conversations: record %s reported FAILED by the channel via a receipt parked during send",
            record.message_id,
        )
        return
    # The record left this worker's hands; its ledger belongs to whoever holds it now.
    logger.warning(
        "conversations: record %s was not moved to provisional after a full send (provisional write returned "
        "%d); the send ledger is left for the worker that owns it now",
        record.message_id,
        outcome,
    )


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
        await _notify_refusal(store, record, channel)


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
        await _notify_refusal(store, record, channel)


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
        await _notify_refusal(store, record, channel)
