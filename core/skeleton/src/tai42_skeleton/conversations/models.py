"""The host-internal answer/delivery record — transient runtime state for one accepted
message. Not a contract type; the wire shapes live in :mod:`tai42_contract.conversations`.

``delivery_status`` and ``answer_status`` are ORTHOGONAL: ``answer_status`` is the nature
of the turn's outcome, fixed when the turn completes; ``delivery_status`` is where that
outcome sits in the send machine.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tai42_contract.conversations import (
    AnswerPart,
    AnswerStatus,
    ConversationAnswer,
    ConversationDoor,
    joined_answer_text,
)


class DeliveryStatus(StrEnum):
    """Where a record sits between intake and a terminal outcome.

    ``accepted`` is pre-turn intake and carries no answer; ``pending_delivery`` is
    persisted-but-unsent (what a re-drive resumes); ``provisional`` is sent and awaiting an
    out-of-band receipt or grace expiry; ``delivered``/``failed``/``shed``/``silent`` are
    terminal and are the only states carrying the retention TTL. ``shed`` ran no turn and
    never sends; ``silent`` ran a tool turn whose reply mapped to nothing and so, by
    design, sends nothing.
    """

    ACCEPTED = "accepted"
    PENDING_DELIVERY = "pending_delivery"
    PROVISIONAL = "provisional"
    DELIVERED = "delivered"
    FAILED = "failed"
    SHED = "shed"
    SILENT = "silent"


#: The states nothing drives further; the retention TTL is applied on reaching one.
TERMINAL_STATUSES = frozenset(
    {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, DeliveryStatus.SHED, DeliveryStatus.SILENT}
)

#: The states carrying no produced answer; every other state carries one.
ANSWERLESS_STATUSES = frozenset({DeliveryStatus.ACCEPTED, DeliveryStatus.SHED, DeliveryStatus.SILENT})


class ConversationRecord(BaseModel):
    """One accepted message's durable record — its admission, the answer its turn produced
    and its delivery state. Frozen: a store read is a snapshot, and a transition is a fresh
    write through the record store's atomic seam.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1)
    route_name: str = Field(min_length=1)
    door: ConversationDoor
    thread_id: str = Field(min_length=1)
    # Embedded verbatim into the thread-index / person-index Redis key names, so an
    # oversized value would form an unbounded key: capped generously above any
    # legitimate address (phone / visitor id / email) and refused loudly past it.
    client_address: str = Field(min_length=1, max_length=256)

    # door=channel delivery target: the channel to notify and the identity to send FROM.
    channel: str | None = None
    our_identity: str | None = None
    # door=channel intake: the provider's id this record was deduped under. ``None`` for an
    # api-door record; never blank, which would share one marker with every other blank id.
    provider_message_id: str | None = Field(default=None, min_length=1)
    # door=api delivery target. The signing secret is NOT stored here — the executor reads
    # it live from the route row at send.
    callback_url: str | None = None

    # The api-door caller the turn was invoked by, and the operator an ``operator`` record
    # was sent by. ``None`` for a channel-door client record, which is then admin-only to
    # read; an ``operator`` record always names its sender, whichever door it rides.
    caller_principal: str | None = None

    # Who produced this record: ``client`` is an inbound message's turn; ``operator`` is a
    # message an operator sent into the thread by hand, which runs no turn. Required — every
    # construction states it, and a stored blob missing it is corruption that fails loudly.
    origin: Literal["client", "operator"]

    # The inbound message this record answers, verbatim — nothing truncates or caps it
    # here, so its size is whatever the door that read it admitted on its own body. A
    # ``client`` record carries the message it answers; an ``operator`` record carries ``""``.
    inbound_text: str

    # ``None`` exactly while the record carries no turn outcome (``accepted``, ``shed``,
    # channel-door ``silent``); set on every state carrying one.
    answer_status: AnswerStatus | None = None
    # Client-facing text; ``None`` for a ``silent`` outcome, and for an ``error`` turn the
    # internal detail lives in ``error``.
    answer: str | None = None
    # The ordered rich :class:`AnswerPart` messages the turn produced when a single joined
    # string would lose something (more than one message, or one carrying media/options/a
    # template). The delivery machine sends each as its own message, in order. ``None`` for a
    # single PLAIN-TEXT answer (``answer`` is then the whole reply): mirrors
    # :class:`ConversationAnswer.parts`, so a present list is non-empty and its part messages
    # join with ``"\n\n"`` to exactly ``answer`` — intake dedup, transcripts and the api-door
    # body all keep reading ``answer``.
    answer_parts: list[AnswerPart] | None = None
    error: str | None = None

    delivery_status: DeliveryStatus = DeliveryStatus.PENDING_DELIVERY
    # Provider-assigned ids of this record's sends, correlated by out-of-band receipts.
    outbound_message_ids: list[str] = Field(default_factory=list)
    attempts: int = 0

    created_at: float
    updated_at: float

    @model_validator(mode="after")
    def _outcome_matches_status(self) -> ConversationRecord:
        """A record carries a turn outcome exactly when its status says it has one, so
        nothing reaching the delivery machine can be missing the outcome it must send.
        An ``answered``/``error`` outcome carries answer text — a string, EMPTY only for an
        all-media answer whose ``answer_parts`` carry the content, in which case ``answer_parts``
        must be present. A ``silent`` one (an api-door no-reply the delivery machine still marks)
        carries none."""
        answerless = self.delivery_status in ANSWERLESS_STATUSES
        if answerless != (self.answer_status is None):
            raise ValueError(
                f"delivery_status {self.delivery_status.value!r} and answer_status "
                f"{self.answer_status!r} disagree on whether this record carries an outcome"
            )
        if self.answer_status in ("answered", "error"):
            if self.answer is None:
                raise ValueError("an answered/error record carries answer text (empty only for an all-media answer)")
            if not self.answer.strip() and not self.answer_parts:
                raise ValueError("an answered/error record with blank answer text must carry media-only answer_parts")
        if self.answer_status == "silent" and self.answer is not None:
            raise ValueError("a silent record carries no answer text")
        return self

    @model_validator(mode="after")
    def _parts_mirror_the_answer(self) -> ConversationRecord:
        """The record's ``answer_parts`` obeys the same invariant the wire
        :class:`ConversationAnswer` does: a present list is non-empty, rides an
        ``answered``/``error`` outcome, and its NON-BLANK part messages join with ``"\n\n"`` to
        exactly ``answer`` (a media-only part contributes nothing) — so the joined text the
        send/transcript/callback paths read and the ordered parts the delivery machine sends can
        never disagree."""
        if self.answer_parts is None:
            return self
        if not self.answer_parts:
            raise ValueError("answer_parts must be a non-empty list when present")
        if self.answer_status not in ("answered", "error"):
            raise ValueError("only an answered/error record carries answer_parts")
        if joined_answer_text(self.answer_parts) != (self.answer or ""):
            raise ValueError("answer must equal the non-blank answer_parts messages joined with a blank line")
        return self

    @model_validator(mode="after")
    def _origin_matches_fields(self) -> ConversationRecord:
        """A ``client`` record answers a non-blank inbound message; an ``operator`` record
        carries no inbound (``""``) and is always an ``answered`` outcome with non-blank
        answer text — it IS the operator's reply, sent into the thread with no turn to run."""
        if self.origin == "operator":
            if self.inbound_text != "":
                raise ValueError("an operator record carries no inbound_text (must be '')")
            if self.answer_status != "answered":
                raise ValueError(f"an operator record is always answered, got answer_status {self.answer_status!r}")
            if not (self.caller_principal or "").strip():
                raise ValueError("an operator record must name the operator that sent it in caller_principal")
        elif not self.inbound_text.strip():
            raise ValueError("a client record carries non-blank inbound_text")
        return self

    def answer_payload(self) -> ConversationAnswer:
        """The :class:`ConversationAnswer` this record delivers — the one shape both the
        signed callback body and the sync-wait payload carry. A ``silent`` outcome carries
        no answer text. Raises on a record with no turn outcome at all."""
        if self.answer_status is None:
            raise RuntimeError(
                f"conversation record {self.message_id!r} is {self.delivery_status.value} and carries no outcome"
            )
        return ConversationAnswer(
            message_id=self.message_id,
            thread_id=self.thread_id,
            status=self.answer_status,
            answer=self.answer,
            parts=self.answer_parts,
        )

    def view(self) -> dict[str, object]:
        """The record as an ADMIN read door returns it. Includes ``error``, the turn's raw
        internal detail, so it is only for a caller with authority over the route's key."""
        return self.model_dump(mode="json")

    def caller_view(self) -> dict[str, object]:
        """The record as the CALLER-scoped read door returns it: the message, its outcome
        and where delivery stands. An allow-list, so a newly added field stays withheld
        until deliberately published here. ``error`` and the delivery bookkeeping are
        withheld — the turn ran as the ROUTE's key, not the caller's. ``inbound_text`` is
        published: it is the text this caller sent.
        """
        return self.model_dump(
            mode="json",
            include={
                "message_id",
                "route_name",
                "door",
                "thread_id",
                "client_address",
                "caller_principal",
                "origin",
                "inbound_text",
                "answer_status",
                "answer",
                "answer_parts",
                "delivery_status",
                "created_at",
                "updated_at",
            },
        )


__all__ = ["ANSWERLESS_STATUSES", "TERMINAL_STATUSES", "ConversationRecord", "DeliveryStatus"]
