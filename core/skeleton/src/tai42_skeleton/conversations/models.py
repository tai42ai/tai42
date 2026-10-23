"""The host-internal answer/delivery record — transient runtime state for one accepted message.

Not a contract type; the wire shapes live in :mod:`tai42_contract.conversations`.

``delivery_status`` and ``answer_status`` are ORTHOGONAL: ``answer_status`` is the nature
of the turn's outcome, fixed when the turn completes; ``delivery_status`` is where that
outcome sits in the send machine.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from tai42_contract.conversations import (
    AnswerPart,
    AnswerStatus,
    ConversationAnswer,
    ConversationDoor,
    ReplyTo,
    joined_answer_text,
)
from tai42_contract.errors import UnavailableError
from tai42_contract.interactions.models import LocationElement, MediaItem, check_media_list
from tai42_contract.locale import normalize_optional_locale


class DeliveryStatus(StrEnum):
    """Where a record sits between intake and a terminal outcome.

    ``accepted`` is pre-turn intake and carries no answer; ``pending_delivery`` is
    persisted-but-unsent (what a re-drive resumes); ``provisional`` is sent and awaiting an
    out-of-band receipt or grace expiry; ``delivered``/``failed``/``shed``/``silent``/
    ``merged``/``superseded`` are terminal and are the only states carrying the retention
    TTL. ``shed`` ran no turn and never sends; ``silent`` ran a tool turn whose reply mapped
    to nothing and so, by design, sends nothing; ``delivered`` on an api record without a
    callback means the answer is readable at the message door and nothing was sent. ``merged``
    and ``superseded`` are the channel-door terminal states of an overlap outcome — a message
    whose text was carried into a later turn (``merged``) or dropped in favour of one
    (``superseded``); each names that turn through ``successor_id`` and, like ``silent``, never
    sends. On the API door an overlap outcome rides ``pending_delivery`` carrying the matching
    ``answer_status`` until its marker is delivered, the ``silent`` split.

    Two states belong to a send record (``origin="send"``) alone. ``sending`` is not terminal:
    the record is written and its send has started; nothing re-sends it, and a row left in it
    past its settle deadline is settled to ``outcome_unknown``. ``outcome_unknown`` is terminal:
    the send started and never reported an outcome (it passed its bound, it was cancelled while
    the medium call was in flight, or the process stopped), so the message may or may not have
    reached the participant; it is never re-sent.
    """

    ACCEPTED = "accepted"
    PENDING_DELIVERY = "pending_delivery"
    PROVISIONAL = "provisional"
    DELIVERED = "delivered"
    FAILED = "failed"
    SHED = "shed"
    SILENT = "silent"
    MERGED = "merged"
    SUPERSEDED = "superseded"
    SENDING = "sending"
    OUTCOME_UNKNOWN = "outcome_unknown"


#: The states nothing drives further; the retention TTL is applied on reaching one.
TERMINAL_STATUSES = frozenset(
    {
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
        DeliveryStatus.SHED,
        DeliveryStatus.SILENT,
        DeliveryStatus.MERGED,
        DeliveryStatus.SUPERSEDED,
        DeliveryStatus.OUTCOME_UNKNOWN,
    }
)

#: The delivery states only a send record (``origin="send"``) takes.
SEND_ONLY_STATUSES = frozenset({DeliveryStatus.SENDING, DeliveryStatus.OUTCOME_UNKNOWN})

#: The states carrying no produced answer; every other state carries one. A channel-door
#: ``merged``/``superseded`` record carries the outcome in its delivery status alone, so its
#: ``answer_status`` is ``None`` here exactly as a channel-door ``silent`` record's is.
ANSWERLESS_STATUSES = frozenset(
    {
        DeliveryStatus.ACCEPTED,
        DeliveryStatus.SHED,
        DeliveryStatus.SILENT,
        DeliveryStatus.MERGED,
        DeliveryStatus.SUPERSEDED,
    }
)

#: The channel-door terminal states of an overlap outcome — the delivery statuses that name a
#: successor turn. The API door instead carries the outcome in ``answer_status`` at
#: ``pending_delivery`` (the ``silent`` split), so the successor rule reads both.
OVERLAP_DELIVERY_STATUSES = frozenset({DeliveryStatus.MERGED, DeliveryStatus.SUPERSEDED})

#: The answer-outcome statuses that name a successor turn, mirroring the contract's overlap
#: outcomes; both carry no answer text and require a ``successor_id``.
SUCCESSOR_ANSWER_STATUSES: frozenset[AnswerStatus] = frozenset({"merged", "superseded"})


#: The path a send record's message took: ``notify`` is ``notify_user``, ``ask`` an
#: interaction's question, ``notice`` a platform notice to the participant, ``channel`` a
#: channel plugin's own send.
SentBy = Literal["notify", "ask", "notice", "channel"]


class SentMessage(BaseModel):
    """One sent part of a record's answer and the provider ids its medium gave it.

    ``part`` indexes the record's ``answer_parts`` (``0`` for a plain-text answer), ``chars``
    is how much of that part this message carried, and ``provider_message_ids`` are the ids an
    out-of-band receipt correlates.
    """

    model_config = ConfigDict(frozen=True)

    part: int = Field(ge=0)
    chars: int = Field(ge=0)
    provider_message_ids: list[str]


class AskSend(BaseModel):
    """The question an ``ask`` send record carries out, and how its delivery attempts went.

    ``sensitive`` is the question's own flag: a reply to it is recorded without its body.
    ``attempts`` counts the delivery attempts that ran; ``timed_out_attempts`` numbers the ones
    that passed their bound with no answer from the medium, each of which may have reached the
    participant.
    """

    model_config = ConfigDict(frozen=True)

    interaction_id: str = Field(min_length=1)
    answer_format: str
    sensitive: bool
    attempts: int = Field(default=0, ge=0)
    timed_out_attempts: list[int] = Field(default_factory=list)


class AskReply(BaseModel):
    """The participant's reply to an ask, as it came over the channel.

    ``answer`` is ``None`` on a sensitive ask: the record keeps that a reply came, when, and its
    ``medium_message_id``, never its body. ``reply_to`` is the earlier message the reply quoted, as
    resolved when the reply was recorded, ``None`` when it quoted none; it is kept on a sensitive
    ask too, since it is about another message.
    """

    model_config = ConfigDict(frozen=True)

    answer: Any | None = None
    medium_message_id: str | None = None
    answered_at: datetime
    reply_to: ReplyTo | None = None


class AskSendView(BaseModel):
    """A record's :class:`AskSend` as a read door serves it.

    An admin read carries every field; the caller-scoped read carries only ``sensitive``, so the
    others are absent from it.
    """

    sensitive: bool
    interaction_id: str | None = Field(default=None, min_length=1)
    answer_format: str | None = None
    attempts: int | None = Field(default=None, ge=0)
    timed_out_attempts: list[int] | None = None


class AskReplyView(BaseModel):
    """A record's :class:`AskReply` as a read door serves it.

    ``answer`` is ``None`` on a sensitive ask. An admin read carries every field; the
    caller-scoped read carries only ``answer`` and ``reply_to``, so the others are absent from it.
    """

    answer: Any | None
    medium_message_id: str | None = None
    answered_at: datetime | None = None
    reply_to: ReplyTo | None = None


class ConversationsIndexLayoutError(RuntimeError):
    """The conversations store holds an index member in a layout this code does not read.

    Raised at boot by the delivery re-drive; the store is reset, never read around.
    """


class PersonThreadClosedError(UnavailableError):
    """A record was refused by a person thread whose person is being erased or no longer exists.

    Nothing was recorded; a message sent again is attributed by its address and lands in its
    route-keyed thread. A 503, so a caller retries.
    """

    def __init__(self, thread_id: str) -> None:
        """Name the refused person thread."""
        super().__init__(
            f"person thread {thread_id} takes no new record: its person is being erased or no longer exists; "
            "nothing was recorded; send it again"
        )
        self.thread_id = thread_id


class ConversationRecord(BaseModel):
    """One accepted message's durable record — its admission, the answer its turn produced, and its delivery state.

    Frozen: a store read is a snapshot, and a transition is a fresh write through
    the record store's atomic seam.
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

    # Who produced this record: ``client`` is an inbound message's turn; ``operator`` names a
    # record an operator's key committed — both a hand-injected operator send and a park
    # completion's resumed reply ride ``operator``; ``send`` is a message the platform sent
    # outside a turn's reply (``sent_by`` names its path). Required — every construction states
    # it, and a stored blob missing it is corruption that fails loudly.
    origin: Literal["client", "operator", "send"]
    # The path a ``send`` record's message took; ``None`` on every other origin.
    sent_by: SentBy | None = None
    # The ``message_id`` of the turn a send ran inside, set only when the send went to that
    # turn's own participant; ``None`` on a send outside a turn and on every other origin.
    turn_message_id: str | None = None

    # ``True`` only for an operator's hand-injected message — the one ``operator`` record that is
    # NOT a reply to a participant's own turn (it runs no turn and answers nothing the
    # participant asked). A client turn and a park-completion resumed reply are both replies the
    # participant awaits, so both carry ``False``. The delivery machine reads this to decide
    # whether a reachable-channel refusal notifies the participant: a refused turn reply does, a
    # refused operator hand-send does not.
    operator_send: bool = False

    # The inbound message this record answers, verbatim — nothing truncates or caps it
    # here, so its size is whatever the door that read it admitted on its own body. A
    # ``client`` record carries the message it answers; an ``operator`` record carries ``""``.
    inbound_text: str
    # The structured submission that rode WITH the inbound text (an ask-less form's
    # answers), bounded by the door's transport checks (``validate_inbound_form``) and
    # stored beside the text as opaque, untrusted participant data — never schema-conformant by
    # promise. ``None`` for a text-only inbound and for every ``operator`` record (an
    # operator send answers, it does not submit).
    inbound_form: dict[str, Any] | None = None
    # The caller-set opaque per-send ``form_tag`` the platform handed out with the form this
    # submission answers, carried back by the channel beside ``inbound_form`` and surfaced to a
    # tool target's payload under the stable ``form_tag`` key. Bounded as transport, never
    # interpreted. ``None`` when the submission carried none, for a text-only inbound, and for
    # every ``operator`` record (an operator send answers, it does not submit).
    inbound_form_tag: str | None = None
    # Structured media the participant sent WITH the inbound text (image/document/video/audio) and a
    # geographic point the participant shared — the inbound counterparts of an answer's media/location,
    # stored beside the text as machine-consumable content. Both surface to a tool target's payload
    # under the stable ``attachments``/``location`` keys. ``None`` when the inbound carried none;
    # an ``operator`` record carries neither (it answers, it does not submit).
    inbound_attachments: list[MediaItem] | None = None
    inbound_location: LocationElement | None = None
    # The earlier message this inbound quotes, resolved from this thread's records when the inbound
    # was accepted. It surfaces to a tool target's payload under the ``reply_to`` key. ``None`` when
    # the inbound quoted none; an ``operator``, ``send`` or ``event`` record never carries one.
    inbound_reply_to: ReplyTo | None = None
    # The participant's BCP 47 locale the channel resolved from its native inbound (a
    # per-message language hint), stored beside the text so the scheduled turn resolves
    # the subject's locale for the rendering layer. ``None`` when the door supplied none
    # (channel with no native hint, api caller that set none, every operator/event record).
    inbound_locale: str | None = None
    # The medium's own id of the inbound message (the id a quote names), ``None`` when the door
    # supplied none.
    inbound_message_id: str | None = None

    # What kind of inbound this record's turn ran on: ``message`` is an inbound text/form
    # turn (the default every channel/api message and operator send carries); ``event`` is
    # a structured turn entered on an existing thread, carrying no human text.
    inbound_kind: Literal["message", "event"] = "message"
    # The structured event payload an ``event`` turn ran on (a ``ConversationEvent`` dump),
    # stored as opaque untrusted data. Present exactly on an ``event`` record; ``None`` on
    # every ``message`` record.
    inbound_event: dict[str, Any] | None = None
    # The principal that AUTHORIZED this record's turn — provenance only, never a key
    # segment and never used to key a thread. An event turn records its authorizing caller
    # here; ``None`` when no distinct authorizer applies.
    submitted_by: str | None = None

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
    # The message_id of the turn that took this record's place: set (non-blank) EXACTLY on a
    # ``merged``/``superseded`` overlap outcome — the channel-door terminal ``merged``/
    # ``superseded`` states or the API-door ``merged``/``superseded`` ``answer_status`` — and
    # ``None`` on every other outcome, mirroring :attr:`ConversationAnswer.successor_id`.
    successor_id: str | None = None

    delivery_status: DeliveryStatus = DeliveryStatus.PENDING_DELIVERY
    # ``True`` when the record's route row had vanished by the time it was accepted, so no
    # transcript names it (the thread indexes were deliberately not written). Written ONCE by
    # the create step's route-gone branch and read back from that hash field; the operator sees
    # the honest marker on the record, not only a log line.
    route_missing: bool = False
    # This record's sent messages, each with its part and the provider ids out-of-band
    # receipts correlate.
    sends: list[SentMessage] = Field(default_factory=list)
    # The question an ``ask`` send carries and its attempts; present exactly when ``sent_by``
    # is ``ask``.
    ask: AskSend | None = None
    # The participant's reply to that ask over the channel; only on an ask send.
    ask_reply: AskReply | None = None
    attempts: int = 0
    # A terminal receipt (``delivered``/``failed``) that arrived while the send was still in
    # flight, staged on the record until the completing provisional write applies it straight to
    # that terminal. ``None`` while none is staged. Written ONLY by the atomic transitions (never
    # by ``create_record``), so a fresh record carries ``None``.
    pending_receipt: DeliveryStatus | None = None

    created_at: float
    updated_at: float

    @field_validator("inbound_locale")
    @classmethod
    def _canonical_inbound_locale(cls, value: str | None) -> str | None:
        return normalize_optional_locale(value)

    @field_validator("inbound_attachments")
    @classmethod
    def _check_inbound_attachments(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        # None means no inbound media; a present list carries the shared list-level media caps
        # (non-empty, item count, summed URI) — each item's own shape is MediaItem's concern.
        if value is not None:
            check_media_list(value)
        return value

    @model_validator(mode="after")
    def _outcome_matches_status(self) -> ConversationRecord:
        """A record carries a turn outcome exactly when its status says it has one.

        So nothing reaching the delivery machine can be missing the outcome it
        must send. An ``answered``/``error`` outcome carries answer text — a
        string, EMPTY only for an all-media answer whose ``answer_parts`` carry
        the content, in which case ``answer_parts`` must be present. A ``silent``
        one (an api-door no-reply the delivery machine still marks) carries none.
        """
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
        if self.delivery_status in SEND_ONLY_STATUSES and self.origin != "send":
            raise ValueError(
                f"only a send record takes delivery_status {self.delivery_status.value!r}, got origin {self.origin!r}"
            )
        return self

    @model_validator(mode="after")
    def _parts_mirror_the_answer(self) -> ConversationRecord:
        r"""The record's ``answer_parts`` obeys the same invariant the wire :class:`ConversationAnswer` does.

        A present list is non-empty, rides an ``answered``/``error`` outcome, and
        its NON-BLANK part messages join with ``"\n\n"`` to exactly ``answer`` (a
        media-only part contributes nothing) — so the joined text the
        send/transcript/callback paths read and the ordered parts the delivery
        machine sends can never disagree.
        """
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
    def _successor_matches_outcome(self) -> ConversationRecord:
        """A ``merged``/``superseded`` record names the turn that took its place; no other record does.

        ``successor_id`` is that turn's ``message_id`` — set (non-blank) EXACTLY on an overlap
        outcome and ``None`` on every other, so the pointer never dangles on an ordinary turn nor
        goes missing where it must resolve the replacement. The outcome shows as the terminal
        ``merged``/``superseded`` delivery status on the channel door and as the
        ``merged``/``superseded`` ``answer_status`` at ``pending_delivery`` on the API door (the
        ``silent`` split), so both forms are recognised here.
        """
        overlap_outcome = (
            self.delivery_status in OVERLAP_DELIVERY_STATUSES or self.answer_status in SUCCESSOR_ANSWER_STATUSES
        )
        if overlap_outcome:
            if self.successor_id is None or not self.successor_id.strip():
                raise ValueError("a merged/superseded record names its successor turn in a non-blank successor_id")
        elif self.successor_id is not None:
            raise ValueError("only a merged/superseded record carries a successor_id")
        return self

    @model_validator(mode="after")
    def _origin_matches_fields(self) -> ConversationRecord:
        """A ``client`` record answers a non-blank inbound; an ``operator`` or ``send`` record carries none.

        Only a ``send`` record carries ``sent_by``, ``turn_message_id`` and the ask fields. An
        ``operator`` record carries no inbound (``""``) and is always an
        ``answered`` outcome with non-blank answer text — it IS the operator's
        reply, sent into the thread with no turn to run. An ``event`` inbound is a
        structured ``client`` turn with no human text: it carries its
        ``inbound_event`` and an empty ``inbound_text``, while a ``message``
        inbound carries no ``inbound_event``.
        """
        if self.origin != "send" and (
            self.sent_by is not None
            or self.turn_message_id is not None
            or self.ask is not None
            or self.ask_reply is not None
        ):
            raise ValueError("only a send record carries sent_by, turn_message_id, ask or ask_reply")
        if self.origin == "send":
            self._check_send_fields()
        elif self.origin == "operator":
            self._check_operator_inbound()
        elif self.inbound_kind == "event":
            self._check_event_inbound()
        elif not self.inbound_text.strip():
            raise ValueError("a client record carries non-blank inbound_text")
        if self.inbound_kind == "message" and self.inbound_event is not None:
            raise ValueError("a message record carries no inbound_event")
        return self

    def _check_operator_inbound(self) -> None:
        """An operator record is a message send carrying no inbound (text/form/media/event).

        It is always an ``answered`` outcome that names the operator that sent it.
        """
        if self.inbound_kind != "message":
            raise ValueError("an operator record is a message send, never an event")
        if self.inbound_text != "":
            raise ValueError("an operator record carries no inbound_text (must be '')")
        if self.inbound_form is not None:
            raise ValueError("an operator record carries no inbound_form (it answers, it does not submit)")
        if self.inbound_form_tag is not None:
            raise ValueError("an operator record carries no inbound_form_tag (it answers, it does not submit)")
        if self.inbound_attachments is not None or self.inbound_location is not None:
            raise ValueError(
                "an operator record carries no inbound attachments/location (it answers, it does not submit)"
            )
        if self.inbound_reply_to is not None:
            raise ValueError("an operator record carries no inbound_reply_to (it answers, it does not submit)")
        if self.answer_status != "answered":
            raise ValueError(f"an operator record is always answered, got answer_status {self.answer_status!r}")
        if not (self.caller_principal or "").strip():
            raise ValueError("an operator record must name the operator that sent it in caller_principal")

    def _check_send_fields(self) -> None:
        """A send record names its path, carries no inbound, and is always an ``answered`` outcome.

        An ``ask`` send carries its :class:`AskSend` and no other send does. A reply over the
        channel proves the question arrived, so ``ask_reply`` never rides ``failed`` or
        ``outcome_unknown``; a sensitive ask's reply carries no body.
        """
        if self.sent_by is None:
            raise ValueError("a send record names the path it took in sent_by")
        if self.inbound_kind != "message":
            raise ValueError("a send record is a message send, never an event")
        if self.inbound_text != "":
            raise ValueError("a send record carries no inbound_text (must be '')")
        if (
            self.inbound_form is not None
            or self.inbound_form_tag is not None
            or self.inbound_attachments is not None
            or self.inbound_location is not None
            or self.inbound_message_id is not None
            or self.inbound_reply_to is not None
        ):
            raise ValueError("a send record carries no inbound form, tag, media, location, message id or reply_to")
        if self.answer_status != "answered":
            raise ValueError(f"a send record is always answered, got answer_status {self.answer_status!r}")
        if (self.sent_by == "ask") != (self.ask is not None):
            raise ValueError("an ask send record carries its ask, and no other send record does")
        self._check_ask_reply()

    def _check_ask_reply(self) -> None:
        """An ``ask_reply`` rides only an ask send that arrived, without the body of a sensitive ask."""
        if self.ask_reply is None:
            return
        if self.ask is None:
            raise ValueError("only an ask send record carries an ask_reply")
        if self.delivery_status in (DeliveryStatus.FAILED, DeliveryStatus.OUTCOME_UNKNOWN):
            raise ValueError(
                f"an ask_reply proves the question arrived, so it never rides delivery_status "
                f"{self.delivery_status.value!r}"
            )
        if self.ask.sensitive and self.ask_reply.answer is not None:
            raise ValueError("a reply to a sensitive ask is recorded without its answer")

    def _check_event_inbound(self) -> None:
        """An event record carries its structured ``inbound_event`` and no human text."""
        if self.inbound_text != "":
            raise ValueError("an event record carries no inbound_text (must be '')")
        if self.inbound_event is None:
            raise ValueError("an event record carries its structured inbound_event")
        if self.inbound_reply_to is not None:
            raise ValueError("an event record carries no inbound_reply_to (it quotes no message)")

    def answer_payload(self) -> ConversationAnswer:
        """The :class:`ConversationAnswer` this record delivers.

        The one shape both the signed callback body and the sync-wait payload
        carry. A ``silent``/``merged``/``superseded`` outcome carries no answer text; a
        ``merged``/``superseded`` one names its successor turn. Raises on a record
        with no turn outcome at all.
        """
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
            successor_id=self.successor_id,
        )

    def view(self) -> dict[str, object]:
        """The record as an ADMIN read door returns it.

        Includes ``error``, the turn's raw internal detail, so it is only for a
        caller with authority over the route's key.
        """
        return self.model_dump(mode="json")

    def caller_view(self) -> dict[str, object]:
        """The record as the CALLER-scoped read door returns it.

        Returns the message, its outcome and where delivery stands. An
        allow-list, so a newly added field stays withheld
        until deliberately published here. ``error`` and the delivery bookkeeping are
        withheld — the turn ran as the ROUTE's key, not the caller's. ``inbound_text``,
        ``inbound_form``, ``inbound_form_tag``, ``inbound_attachments``, ``inbound_location``,
        ``inbound_reply_to``, ``inbound_kind``, ``inbound_event`` and ``submitted_by`` are published:
        they are the message (and the structured submission / tag / media / location / quoted
        message / event) this caller sent. A send record publishes its ``sent_by`` and
        ``turn_message_id``, whether its ask was ``sensitive`` and the reply's ``answer`` (``None``
        on a sensitive ask) and ``reply_to``; its ``sends`` are delivery bookkeeping and stay
        withheld.
        """
        return self.model_dump(
            mode="json",
            include={
                "message_id": True,
                "route_name": True,
                "door": True,
                "thread_id": True,
                "client_address": True,
                "caller_principal": True,
                "origin": True,
                "sent_by": True,
                "turn_message_id": True,
                "inbound_text": True,
                "inbound_form": True,
                "inbound_form_tag": True,
                "inbound_attachments": True,
                "inbound_location": True,
                "inbound_reply_to": True,
                "inbound_kind": True,
                "inbound_event": True,
                "submitted_by": True,
                "answer_status": True,
                "answer": True,
                "answer_parts": True,
                "successor_id": True,
                "ask": {"sensitive"},
                "ask_reply": {"answer", "reply_to"},
                "delivery_status": True,
                "route_missing": True,
                "created_at": True,
                "updated_at": True,
            },
        )


__all__ = [
    "ANSWERLESS_STATUSES",
    "OVERLAP_DELIVERY_STATUSES",
    "SEND_ONLY_STATUSES",
    "SUCCESSOR_ANSWER_STATUSES",
    "TERMINAL_STATUSES",
    "AskReply",
    "AskReplyView",
    "AskSend",
    "AskSendView",
    "ConversationRecord",
    "DeliveryStatus",
    "SentBy",
    "SentMessage",
]
