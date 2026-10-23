"""The one recorder of a message the platform sends outside a turn's reply.

Every such send (``notify_user``, an ask, a platform notice, a channel plugin's own send) goes
through :func:`recorded_send`: the send record is written in ``sending`` before the send goes
out, the send runs, and the record is completed from the send's :class:`SendOutcome` or ended
``failed`` / ``outcome_unknown``. A send record is never re-sent.

A send is attributed to the thread an inbound from its participant would use: the route bound to
the send's channel and identity, then the participant's thread on it (the linked person's
thread for a linked address). A send inside a turn to that turn's own participant is placed in
the turn's thread and names the turn in ``turn_message_id``. A send with no route, or on a
deployment with no conversations store, is outside any conversation and goes out unrecorded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from tai42_contract.channels import ChannelAddress, ChannelDeliveryError, ChannelNotification, SendOutcome
from tai42_contract.conversations import AnswerPart, current_conversation_turn
from tai42_contract.monitoring import MonitoringLevel

from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.models import (
    AskSend,
    ConversationRecord,
    DeliveryStatus,
    SentBy,
    SentMessage,
)
from tai42_skeleton.conversations.record_send import SendEndStatus, SendPlacement, send_placement_warning
from tai42_skeleton.conversations.settings import conversations_settings
from tai42_skeleton.monitoring import get_monitoring

if TYPE_CHECKING:
    from tai42_contract.conversations import ConversationRoute

    from tai42_skeleton.conversations.records import ConversationRecordStore

logger = logging.getLogger(__name__)


@dataclass
class SendAttempts:
    """An ask's delivery attempts, as its retry loop reports them to the recorder.

    The loop calls :meth:`start` just before each medium call and :meth:`failed` when that call
    raises; a call that returns or is cancelled leaves the mark as it is, so ``in_flight`` is
    true exactly while a medium call runs, and stays true when a cancellation lands inside one.
    """

    count: int = 0
    timed_out: list[int] = field(default_factory=list)
    in_flight: bool = False
    last_error: str | None = None

    def start(self) -> None:
        """Mark one more attempt as going out."""
        self.count += 1
        self.in_flight = True

    def returned(self) -> None:
        """Clear the in-flight mark of an attempt whose medium call returned."""
        self.in_flight = False

    def failed(self, error: BaseException, *, timed_out: bool) -> None:
        """Record the current attempt's failure; ``timed_out`` when it passed its bound unanswered."""
        self.in_flight = False
        self.last_error = _error_text(error)
        if timed_out:
            self.timed_out.append(self.count)

    def end_status(self) -> SendEndStatus:
        """How the record ends by the attempt history: unknown once an attempt timed out, else failed."""
        return DeliveryStatus.OUTCOME_UNKNOWN if self.timed_out else DeliveryStatus.FAILED

    def ask(self, ask: AskSend) -> AskSend:
        """``ask`` carrying the attempts as they stand."""
        return ask.model_copy(update={"attempts": self.count, "timed_out_attempts": list(self.timed_out)})


@dataclass(frozen=True)
class _Placement:
    """Where a send is attributed: its route, thread, address and the turn it ran inside."""

    route: ConversationRoute
    our_identity: str
    client_address: str
    thread_id: str
    turn_message_id: str | None


def _error_text(error: BaseException) -> str:
    detail = str(error)
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def notification_part(notification: ChannelNotification) -> AnswerPart:
    """The :class:`AnswerPart` a notification's content surface records as."""
    return AnswerPart.model_validate({name: getattr(notification, name) for name in AnswerPart.model_fields})


async def _thread_for(route: ConversationRoute, address: str) -> str:
    from tai42_skeleton.conversations.turn.routing import _multichannel_context, _resolve_thread_id

    multichannel = await _multichannel_context(
        route,
        door="channel",
        channel=route.channel,
        our_identity=route.our_identity,
        address=address,
        accountable=address,
    )
    return await _resolve_thread_id(route, multichannel, address)


async def _attribute(store: ConversationRecordStore, route: ConversationRoute, address: str) -> _Placement:
    """The thread of ``address`` on ``route``, or the ambient turn's own thread when it is that turn's participant."""
    client_address = canonical_address(address)
    thread_id = await _thread_for(route, client_address)
    turn_message_id = None
    turn = current_conversation_turn()
    if turn is not None and turn.route_name == route.route_name:
        lead = await store.get_record(turn.message_id)
        if lead is not None and lead.route_name == route.route_name and lead.client_address == client_address:
            thread_id = turn.thread_id
            turn_message_id = turn.message_id
    if route.our_identity is None:
        raise RuntimeError(f"conversations: channel route {route.route_name!r} carries no our_identity")
    return _Placement(
        route=route,
        our_identity=route.our_identity,
        client_address=client_address,
        thread_id=thread_id,
        turn_message_id=turn_message_id,
    )


async def _route_for(channel: str, our_identity: str) -> ConversationRoute | None:
    from tai42_skeleton.conversations.turn.errors import ConversationRouteResolutionError
    from tai42_skeleton.conversations.turn.routing import _resolve_channel_route

    try:
        return await _resolve_channel_route(channel, canonical_address(our_identity))
    except ConversationRouteResolutionError:
        return None


def _answer_fields(parts: list[AnswerPart]) -> tuple[str, list[AnswerPart] | None]:
    from tai42_skeleton.conversations.turn.record import _answer_fields as answer_fields

    return answer_fields(parts)


def _begin_record(
    placement: _Placement, *, channel: str, parts: list[AnswerPart], sent_by: SentBy, ask: AskSend | None
) -> ConversationRecord:
    answer, answer_parts = _answer_fields(parts)
    now = time.time()
    return ConversationRecord(
        message_id=str(uuid4()),
        route_name=placement.route.route_name,
        door="channel",
        thread_id=placement.thread_id,
        client_address=placement.client_address,
        channel=channel,
        our_identity=placement.our_identity,
        origin="send",
        sent_by=sent_by,
        turn_message_id=placement.turn_message_id,
        inbound_text="",
        answer_status="answered",
        answer=answer,
        answer_parts=answer_parts,
        delivery_status=DeliveryStatus.SENDING,
        ask=ask,
        created_at=now,
        updated_at=now,
    )


def _who(record: ConversationRecord) -> str:
    return f"send record {record.message_id} on {record.channel} to {record.client_address}"


def _log_placement(record: ConversationRecord, placement: SendPlacement) -> None:
    warning = send_placement_warning(record, placement)
    if warning is not None:
        logger.warning("conversations: %s", warning)
    elif placement.code == -2:
        logger.error(
            "conversations: %s was no longer sending (%s) when its send answered; its settle deadline passed "
            "during the send and the record keeps %s",
            _who(record),
            placement.detail,
            placement.detail,
        )


async def _complete(
    store: ConversationRecordStore,
    begun: ConversationRecord,
    route: ConversationRoute,
    outcome: SendOutcome,
    ask: AskSend | None,
) -> None:
    """Complete ``begun`` from ``outcome``, re-attributed by the outcome's address on the begin route.

    A completion write that fails is logged at ERROR with the sent ids and marked on the active
    span, never raised: the medium accepted the send, and a raise would make the caller send it
    again. The row stays ``sending`` and the settle ends it ``outcome_unknown``.
    """
    ids = list(outcome.message_ids)
    try:
        if canonical_address(outcome.our_identity) != begun.our_identity:
            logger.error(
                "conversations: %s: the send reported identity %r, not the identity %r it was begun from; "
                "the record keeps its begin route %r",
                _who(begun),
                outcome.our_identity,
                begun.our_identity,
                route.route_name,
            )
        placement = await _attribute(store, route, outcome.address)
        completed = begun.model_copy(
            update={
                "client_address": placement.client_address,
                "thread_id": placement.thread_id,
                "turn_message_id": placement.turn_message_id,
                "sends": [SentMessage(part=0, chars=len(begun.answer or ""), provider_message_ids=ids)] if ids else [],
                "ask": ask if ask is not None else begun.ask,
                "updated_at": time.time(),
            }
        )
        result = await store.complete_send_record(completed, route=route)
    except Exception as exc:
        logger.exception(
            "conversations: %s: the send was accepted with ids %r but its record could not be completed; it stays "
            "sending until the settle ends it outcome_unknown",
            _who(begun),
            ids,
        )
        get_monitoring().writer.update_current_span(
            level=MonitoringLevel.ERROR,
            status_message=f"{_who(begun)} not completed after the send was accepted: {_error_text(exc)}",
        )
        return
    _log_placement(completed, result)
    if result.code in (0, 1) and result.detail == DeliveryStatus.PROVISIONAL.value:
        from tai42_skeleton.conversations import delivery as _pkg

        _pkg._spawn(_pkg._confirm_after_grace(completed.message_id, store.settings.delivery_grace_seconds))


async def _end(
    store: ConversationRecordStore,
    begun: ConversationRecord,
    *,
    status: SendEndStatus,
    error: str,
    ask: AskSend | None,
) -> None:
    """End ``begun`` as ``status`` in its begin thread; a failing end write is logged at ERROR, never raised.

    The send's own result (its error or cancellation) is what the caller gets; the row stays
    ``sending`` and the settle ends it ``outcome_unknown``.
    """
    record = begun if ask is None else begun.model_copy(update={"ask": ask})
    try:
        result = await store.end_send_record(record, status=status, error=error)
    except Exception:
        logger.exception(
            "conversations: %s: its end as %s could not be written; it stays sending until the settle ends it "
            "outcome_unknown",
            _who(begun),
            status.value,
        )
        return
    _log_placement(record, result)


def _check_ask(ask: AskSend | None, attempts: SendAttempts | None) -> None:
    if (ask is None) != (attempts is None):
        raise ValueError("an ask send is recorded with its attempts, and only an ask send carries them")


async def recorded_send(
    *,
    channel: str,
    address: ChannelAddress,
    parts: list[AnswerPart],
    sent_by: SentBy,
    send: Callable[[], Awaitable[SendOutcome | None]],
    bound: float,
    ask: AskSend | None = None,
    attempts: SendAttempts | None = None,
) -> SendOutcome | None:
    """Record ``send`` as a send record, record first, and return what it returned.

    ``address`` is the channel's pre-send address (``Channel.resolve_address``); it places a send
    that reports no outcome. ``bound`` is the send's bound: the recorder holds a recorded single
    send to it, and the settle deadline is the begin time plus ``bound`` plus the delivery claim lease.
    A single send past ``bound`` ends ``outcome_unknown`` and raises a non-retryable
    :class:`ChannelDeliveryError`, so no caller retry sends it twice.

    An ask passes its :class:`AskSend` and the :class:`SendAttempts` its retry loop reports to;
    ``send`` is that whole loop, which bounds each attempt itself and returns ``None`` when the
    question was answered or gone before an attempt succeeded. Its record ends by its attempts.

    A send with no route, or with no conversations store, goes out unrecorded and unbounded.
    Raises what the send raised, unchanged; a failure to write the ``sending`` record raises
    before anything is sent.
    """
    _check_ask(ask, attempts)
    if conversations_settings().in_memory:
        logger.debug("conversations: no conversations store; %s send on %r goes out unrecorded", sent_by, channel)
        return await send()
    route = await _route_for(channel, address.our_identity)
    if route is None:
        logger.debug(
            "conversations: no channel route for %r identity %r; %s send goes out unrecorded",
            channel,
            address.our_identity,
            sent_by,
        )
        return await send()
    store = cache.get_conversations_manager().records
    placement = await _attribute(store, route, address.address)
    begun = _begin_record(placement, channel=channel, parts=parts, sent_by=sent_by, ask=ask)
    settle_deadline = begun.created_at + bound + store.settings.delivery_claim_lease_seconds
    await store.begin_send_record(begun, route=route, settle_deadline=settle_deadline)
    if ask is not None and attempts is not None:
        return await _run_ask(store, begun, route, send, ask, attempts)
    return await _run_single(store, begun, route, send, bound)


async def _run_single(
    store: ConversationRecordStore,
    begun: ConversationRecord,
    route: ConversationRoute,
    send: Callable[[], Awaitable[SendOutcome | None]],
    bound: float,
) -> SendOutcome:
    try:
        async with asyncio.timeout(bound) as scope:
            outcome = await send()
    except TimeoutError as exc:
        if not scope.expired():
            await _end(store, begun, status=DeliveryStatus.FAILED, error=_error_text(exc), ask=None)
            raise
        error = f"the send did not answer within {bound:g}s; the outcome is unknown"
        logger.warning("conversations: %s: %s; not re-sent", _who(begun), error)
        await _end(store, begun, status=DeliveryStatus.OUTCOME_UNKNOWN, error=error, ask=None)
        raise ChannelDeliveryError(
            f"{_who(begun)}: {error}; it may have reached the participant and is not retried",
            retryable=False,
        ) from exc
    except Exception as exc:
        await _end(store, begun, status=DeliveryStatus.FAILED, error=_error_text(exc), ask=None)
        raise
    except BaseException as exc:
        error = (
            f"the send was cancelled while the medium call was in flight ({_error_text(exc)}); the outcome is unknown"
        )
        logger.warning("conversations: %s: %s; not re-sent", _who(begun), error)
        await _end(store, begun, status=DeliveryStatus.OUTCOME_UNKNOWN, error=error, ask=None)
        raise
    if outcome is None:
        raise RuntimeError(f"{_who(begun)}: the send returned no outcome; only an ask's loop may")
    await _complete(store, begun, route, outcome, None)
    return outcome


def _warn_timed_out(record: ConversationRecord, ask: AskSend, attempts: SendAttempts, what: str) -> None:
    if attempts.timed_out:
        logger.warning(
            "conversations: %s for interaction %s: delivery attempts %s timed out unanswered; %s",
            _who(record),
            ask.interaction_id,
            attempts.timed_out,
            what,
        )


async def _end_by_history(
    store: ConversationRecordStore,
    begun: ConversationRecord,
    ask: AskSend,
    attempts: SendAttempts,
    *,
    error: str,
) -> None:
    status = attempts.end_status()
    _warn_timed_out(begun, ask, attempts, "the participant may have received the question")
    await _end(store, begun, status=status, error=error, ask=attempts.ask(ask))


async def _run_ask(
    store: ConversationRecordStore,
    begun: ConversationRecord,
    route: ConversationRoute,
    send: Callable[[], Awaitable[SendOutcome | None]],
    ask: AskSend,
    attempts: SendAttempts,
) -> SendOutcome | None:
    try:
        outcome = await send()
    except Exception as exc:
        await _end_by_history(store, begun, ask, attempts, error=_error_text(exc))
        raise
    except BaseException as exc:
        if attempts.in_flight:
            error = (
                f"the ask was cancelled while delivery attempt {attempts.count} was in flight "
                f"({_error_text(exc)}); the outcome is unknown"
            )
            logger.warning("conversations: %s: %s; not re-sent", _who(begun), error)
            _warn_timed_out(begun, ask, attempts, "the participant may have received the question")
            await _end(store, begun, status=DeliveryStatus.OUTCOME_UNKNOWN, error=error, ask=attempts.ask(ask))
        else:
            last = attempts.last_error or "no attempt went out"
            await _end_by_history(
                store, begun, ask, attempts, error=f"{last}; the ask was then cancelled ({_error_text(exc)})"
            )
        raise
    if outcome is None:
        # The question was answered or gone before an attempt succeeded; with no attempt sent, the
        # history is empty and the record ends ``failed``: nothing reached the medium.
        error = attempts.last_error or "no delivery attempt went out: the question was answered or gone first"
        await _end_by_history(store, begun, ask, attempts, error=error)
        return None
    _warn_timed_out(begun, ask, attempts, "the participant may have received the question more than once")
    await _complete(store, begun, route, outcome, attempts.ask(ask))
    return outcome


async def send_recorded(
    *,
    channel: str,
    our_identity: str,
    client_address: str,
    content: AnswerPart,
    send: Callable[[], Awaitable[SendOutcome]],
) -> SendOutcome:
    """Record a send a channel plugin makes itself as a ``channel`` send record, and return its outcome.

    ``our_identity`` and ``client_address`` are the plugin's pre-send address; the send is
    bounded by ``delivery_send_timeout_seconds``.
    """
    outcome = await recorded_send(
        channel=channel,
        address=ChannelAddress(our_identity=our_identity, address=client_address),
        parts=[content],
        sent_by="channel",
        send=send,
        bound=conversations_settings().delivery_send_timeout_seconds,
    )
    if outcome is None:
        raise RuntimeError(f"channel {channel!r} send returned no outcome")
    return outcome


__all__ = ["SendAttempts", "notification_part", "recorded_send", "send_recorded"]
