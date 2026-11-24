"""Mint and transition the durable :class:`ConversationRecord` for a turn.

A door commits a freshly minted intake record; the turn then transitions it to its
outcome — a produced line to ``pending_delivery``, or an outcome that sends no text (a silent
no-reply, a failure owed no line) to its door-appropriate shape.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from tai42_contract.conversations import (
    AnswerPart,
    AnswerStatus,
    ConversationDoor,
    ConversationRoute,
    ReplyTo,
    joined_answer_text,
)
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn.outcome import (
    _FailedOutcome,
    _ResolvedOutcome,
    _SilentOutcome,
    _SupersededOutcome,
    _text_part,
    _ToolOutcome,
    failure_line,
)

logger = logging.getLogger("tai42_skeleton.conversations.turn")


def _new_record(
    *,
    route: ConversationRoute,
    message_id: str,
    thread_id: str,
    client_address: str,
    caller_principal: str | None,
    provider_message_id: str | None,
    delivery_status: DeliveryStatus,
    inbound_text: str,
    answer_status: AnswerStatus | None = None,
    answer: str | None = None,
    answer_parts: list[AnswerPart] | None = None,
    error: str | None = None,
    origin: Literal["client", "operator"] = "client",
    operator_send: bool = False,
    inbound_form: dict[str, Any] | None = None,
    inbound_form_tag: str | None = None,
    inbound_attachments: list[MediaItem] | None = None,
    inbound_location: LocationElement | None = None,
    inbound_reply_to: ReplyTo | None = None,
    inbound_locale: str | None = None,
    inbound_message_id: str | None = None,
    inbound_kind: Literal["message", "event"] = "message",
    inbound_event: dict[str, Any] | None = None,
    submitted_by: str | None = None,
) -> ConversationRecord:
    """A freshly minted record for one accepted message, in the state its door commits it to.

    That state is ``accepted``, ``pending_delivery`` or ``shed``; a parked turn's completion record
    is minted straight into its outcome (:func:`_outcome_fields`), terminal ``silent`` included.
    ``inbound_text`` is the message verbatim, durable from here so the record reads as a turn of a conversation and not
    only as its answer; ``inbound_form`` is the structured submission that rode WITH it
    (an ask-less form's answers), ``inbound_form_tag`` the caller-set opaque per-send tag that
    rode back with that submission, ``inbound_attachments``/``inbound_location`` are the media
    and location the participant sent with it, ``inbound_reply_to`` the message it quotes as the
    door resolved it, and ``inbound_message_id`` the id the medium gave it — all stored beside
    the text, ``None`` for an ordinary text-only inbound. A ``client`` api-door record MUST name
    the authenticated caller its thread and rate bucket are keyed by, and a ``client``
    channel-door record names none; an ``operator`` record names the operator
    that sent it on EITHER door — it rides no rate bucket and carries no inbound to dedupe.
    """
    if origin == "operator":
        if not (caller_principal and caller_principal.strip()):
            raise RuntimeError(
                f"conversations: an operator record on route {route.route_name!r} requires the sending "
                "operator principal in caller_principal"
            )
    elif (route.door == "api") != bool(caller_principal and caller_principal.strip()):
        raise RuntimeError(
            f"conversations: a {route.door} record cannot carry caller_principal={caller_principal!r}; "
            "the api door requires one and the channel door has none"
        )
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name=route.route_name,
        door=route.door,
        thread_id=thread_id,
        client_address=client_address,
        channel=route.channel,
        our_identity=route.our_identity,
        callback_url=route.callback_url,
        caller_principal=caller_principal,
        origin=origin,
        operator_send=operator_send,
        provider_message_id=provider_message_id,
        inbound_text=inbound_text,
        inbound_form=inbound_form,
        inbound_form_tag=inbound_form_tag,
        inbound_attachments=inbound_attachments,
        inbound_location=inbound_location,
        inbound_reply_to=inbound_reply_to,
        inbound_locale=inbound_locale,
        inbound_message_id=inbound_message_id,
        inbound_kind=inbound_kind,
        inbound_event=inbound_event,
        submitted_by=submitted_by,
        delivery_status=delivery_status,
        answer_status=answer_status,
        answer=answer,
        answer_parts=answer_parts,
        error=error,
        created_at=now,
        updated_at=now,
    )


def _answer_fields(parts: list[AnswerPart]) -> tuple[str, list[AnswerPart] | None]:
    """The ``(answer, answer_parts)`` a record stores for an ordered ``parts`` list.

    The part MESSAGES are joined into the whole text every reader of the flat ``answer``
    consumes, and the parts list ITSELF is stored only when it adds something over that
    text — more than one part, or one part carrying media/options/a template. A single
    PLAIN-TEXT answer carries ``answer_parts=None``, mirroring ``ConversationAnswer.parts``.
    A media-only part contributes nothing to ``answer``, so an all-media answer stores
    ``answer=""`` with the parts.
    """
    answer = joined_answer_text(parts)
    if len(parts) == 1 and parts[0].is_plain_text():
        return answer, None
    return answer, parts


def _line_fields(parts: list[AnswerPart], error_detail: str | None) -> dict[str, Any]:
    """The outcome fields of a turn that delivers ``parts``: ``pending_delivery`` with the answer.

    ``parts`` is the ordered message list; the record stores the joined ``answer`` and, for a
    multi-message answer, the ``answer_parts`` the delivery machine sends one message at a time.
    ``error_detail`` set marks the outcome ``error`` (a failed turn's line, the detail kept on the
    record); ``None`` marks it ``answered``.
    """
    answer, answer_parts = _answer_fields(parts)
    return {
        "answer_status": "answered" if error_detail is None else "error",
        "answer": answer,
        "answer_parts": answer_parts,
        "error": error_detail,
        "delivery_status": DeliveryStatus.PENDING_DELIVERY,
    }


def _nothing_sent_fields(
    door: ConversationDoor,
    *,
    channel_answer_status: AnswerStatus | None,
    api_answer_status: AnswerStatus,
    detail: str | None,
) -> dict[str, Any]:
    """The outcome fields of a turn that sends the participant no text, per door.

    The channel door is terminal ``silent`` carrying ``channel_answer_status``: nothing is ever
    sent. The API door sits at ``pending_delivery`` carrying ``api_answer_status``: the same durable
    machine an answer takes delivers it as a marker with no answer (a signed callback when the
    route declares one, else terminal-readable for the poll door). ``detail`` is the internal
    detail kept in the record's ``error`` field, never sent.
    """
    if door == "channel":
        answer_status, delivery_status = channel_answer_status, DeliveryStatus.SILENT
    else:
        answer_status, delivery_status = api_answer_status, DeliveryStatus.PENDING_DELIVERY
    return {
        "answer_status": answer_status,
        "answer": None,
        "answer_parts": None,
        "error": detail,
        "delivery_status": delivery_status,
    }


def _outcome_fields(
    door: ConversationDoor,
    outcome: _ResolvedOutcome | _SilentOutcome | _FailedOutcome,
    route: ConversationRoute | None,
    *,
    inbound_kind: Literal["message", "event"],
) -> dict[str, Any]:
    """The outcome fields a turn's record carries for a delivered, silent or failed ``outcome``.

    The one seam every completed turn takes — a live turn, a re-driven stranded one and a parked
    turn's late completion alike. An answer goes to ``pending_delivery``. A silent outcome (a
    designed no-reply; ``note`` the optional internal reason, such as a paused run's pending reply)
    sends nothing. A failed outcome is ``error`` with its detail kept on the record; the line it
    delivers is :func:`failure_line` on ``route`` and who sent the turn (``inbound_kind``) — a line
    goes to ``pending_delivery`` behind any leading parts, and no line sends nothing exactly as a
    silent outcome does (an ``error`` marker with no answer on the api door).
    """
    if isinstance(outcome, _SilentOutcome):
        return _nothing_sent_fields(door, channel_answer_status=None, api_answer_status="silent", detail=outcome.note)
    if isinstance(outcome, _FailedOutcome):
        line = failure_line(route, outcome, inbound_kind=inbound_kind)
        if line is None:
            if outcome.leading_parts:
                raise RuntimeError("conversations: a failure that owes no line carries parts to deliver ahead of it")
            return _nothing_sent_fields(
                door, channel_answer_status="error", api_answer_status="error", detail=outcome.error
            )
        return _line_fields([*outcome.leading_parts, _text_part(line)], outcome.error)
    return _line_fields(outcome.parts, None)


def _overlap_record(
    intake: ConversationRecord,
    successor_id: str,
    *,
    channel_status: DeliveryStatus,
    api_answer_status: AnswerStatus,
) -> ConversationRecord:
    """``intake`` resolved to a ``merged``/``superseded`` overlap outcome, per door.

    The channel door records the outcome in its terminal delivery status (``merged``/
    ``superseded``, no answer_status), nothing ever sent; the API door sits the record at
    ``pending_delivery`` carrying the matching ``answer_status`` and successor, delivered as a
    marker exactly as a ``silent`` outcome is (the ``silent`` split). Either way the record names
    its successor turn and carries no answer.
    """
    if intake.door == "channel":
        overlay = {"answer_status": None, "delivery_status": channel_status}
    else:
        overlay = {"answer_status": api_answer_status, "delivery_status": DeliveryStatus.PENDING_DELIVERY}
    return ConversationRecord.model_validate(
        intake.model_dump()
        | overlay
        | {
            "answer": None,
            "answer_parts": None,
            "error": None,
            "successor_id": successor_id,
            "updated_at": time.time(),
        }
    )


def _merged_record(intake: ConversationRecord, successor_id: str) -> ConversationRecord:
    """``intake`` resolved to ``merged`` — its text was carried into the turn ``successor_id`` names."""
    return _overlap_record(intake, successor_id, channel_status=DeliveryStatus.MERGED, api_answer_status="merged")


def _superseded_record(intake: ConversationRecord, successor_id: str) -> ConversationRecord:
    """``intake`` resolved to ``superseded`` — dropped in favour of the turn ``successor_id`` names."""
    return _overlap_record(
        intake, successor_id, channel_status=DeliveryStatus.SUPERSEDED, api_answer_status="superseded"
    )


def _outcome_record(
    intake: ConversationRecord, outcome: _ToolOutcome, route: ConversationRoute | None
) -> ConversationRecord:
    """``intake`` moved to the completed record of a resolved outcome.

    A yielded outcome is ``superseded`` (a terminal on the channel door, a marker on the api door)
    naming its successor; every other outcome takes :func:`_outcome_fields` for the intake's door
    and who sent it.
    """
    if isinstance(outcome, _SupersededOutcome):
        return _superseded_record(intake, outcome.successor_id)
    return ConversationRecord.model_validate(
        intake.model_dump()
        | _outcome_fields(intake.door, outcome, route, inbound_kind=intake.inbound_kind)
        | {"updated_at": time.time()}
    )


def _log_unvoiced_failure(record: ConversationRecord) -> None:
    """Log at ERROR a stored record whose turn failed and sends the participant no line.

    Such a failure reaches no one through the conversation, so its detail is named here beside the
    record that keeps it; a failure that delivers a line is logged where it happened. Called only
    once the record's write has landed, so the line never names an outcome that was not stored.
    """
    if record.answer_status == "error" and record.answer is None:
        logger.error(
            "conversations: the %s turn %s on route %r failed and owes the participant no line; nothing is "
            "sent to them and the record carries the error: %s",
            record.inbound_kind,
            record.message_id,
            record.route_name,
            record.error,
        )
