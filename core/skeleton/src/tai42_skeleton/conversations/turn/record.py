"""Mint and transition the durable :class:`ConversationRecord` for a turn.

A door commits a freshly minted intake record; the turn then transitions it to its
outcome — a produced answer to ``pending_delivery`` or a silent no-reply to its
door-appropriate terminal.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from tai42_contract.conversations import AnswerPart, AnswerStatus, ConversationRoute, joined_answer_text
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn.outcome import _SilentOutcome, _SupersededOutcome, _ToolOutcome


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
    inbound_attachments: list[MediaItem] | None = None,
    inbound_location: LocationElement | None = None,
    inbound_locale: str | None = None,
    inbound_kind: Literal["message", "event"] = "message",
    inbound_event: dict[str, Any] | None = None,
    submitted_by: str | None = None,
) -> ConversationRecord:
    """A freshly minted record for one accepted message, in the state its door commits it to.

    That state is ``accepted``, ``pending_delivery`` or ``shed``. ``inbound_text`` is the message
    verbatim, durable from here so the record reads as a turn of a conversation and not
    only as its answer; ``inbound_form`` is the structured submission that rode WITH it
    (an ask-less form's answers), and ``inbound_attachments``/``inbound_location`` are the media
    and location the participant sent with it — all stored beside the text, ``None`` for an ordinary
    text-only inbound. A ``client`` api-door record MUST name the authenticated caller its
    thread and rate bucket are keyed by, and a ``client`` channel-door record names none; an
    ``operator`` record names the operator that sent it on EITHER door — it rides no rate
    bucket and carries no inbound to dedupe.
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
        inbound_attachments=inbound_attachments,
        inbound_location=inbound_location,
        inbound_locale=inbound_locale,
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


def _with_outcome(
    intake: ConversationRecord, answer_status: AnswerStatus, parts: list[AnswerPart], error_detail: str | None
) -> ConversationRecord:
    """``intake`` carrying a produced outcome and moved to ``pending_delivery``.

    The shape :meth:`ConversationRecordStore.complete_turn` requires. ``parts`` is the ordered
    message list; the record stores the joined ``answer`` and, for a multi-message answer,
    the ``answer_parts`` the delivery machine sends one message at a time.
    """
    answer, answer_parts = _answer_fields(parts)
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": answer_status,
            "answer": answer,
            "answer_parts": answer_parts,
            "error": error_detail,
            "delivery_status": DeliveryStatus.PENDING_DELIVERY,
            "updated_at": time.time(),
        }
    )


def _with_channel_silent(intake: ConversationRecord, note: str | None = None) -> ConversationRecord:
    """``intake`` moved to terminal ``silent`` — a CHANNEL-door tool turn that produced no reply.

    Nothing is ever sent. Carries no answer_status, matching
    :data:`ANSWERLESS_STATUSES`. ``note`` is an optional internal detail (never sent) — the
    paused-run pending reason — recorded so a silent-because-pending turn is diagnosable;
    ``None`` records no detail, exactly as an ordinary silent turn does.
    """
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": None,
            "answer": None,
            "error": note,
            "delivery_status": DeliveryStatus.SILENT,
            "updated_at": time.time(),
        }
    )


def _with_api_silent(intake: ConversationRecord, note: str | None = None) -> ConversationRecord:
    """``intake`` moved to ``pending_delivery`` carrying a ``silent`` outcome — an API-door turn with no reply.

    The silent outcome rides the same durable machine an
    answer takes (a signed callback when the route declares one, else terminal-readable for
    the poll door); it carries no answer text. ``note`` is an optional internal detail (never sent — the
    silent marker delivered to the caller carries no answer or error) — the paused-run pending reason —
    recorded so a silent-because-pending turn is diagnosable; ``None`` records no detail.
    """
    return ConversationRecord.model_validate(
        intake.model_dump()
        | {
            "answer_status": "silent",
            "answer": None,
            "error": note,
            "delivery_status": DeliveryStatus.PENDING_DELIVERY,
            "updated_at": time.time(),
        }
    )


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


def _outcome_record(intake: ConversationRecord, outcome: _ToolOutcome) -> ConversationRecord:
    """Build the completed record from a resolved outcome.

    An answer goes to ``pending_delivery``; a silent outcome is terminal
    ``silent`` on the channel door and a deliverable ``silent`` marker on the api
    door; a yielded outcome is ``superseded`` (a terminal on the channel door, a
    marker on the api door) naming its successor.
    """
    if isinstance(outcome, _SupersededOutcome):
        return _superseded_record(intake, outcome.successor_id)
    if isinstance(outcome, _SilentOutcome):
        if intake.door == "channel":
            return _with_channel_silent(intake, outcome.note)
        return _with_api_silent(intake, outcome.note)
    return _with_outcome(intake, outcome.answer_status, outcome.parts, outcome.error)
