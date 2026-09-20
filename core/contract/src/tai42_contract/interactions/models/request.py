"""The durable ``ask_user`` question model.

``InteractionRequest`` is the one durable question written to a per-group stream, with
its per-answer-format payload validation and its sync/async park discipline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, field_validator, model_validator

from tai42_contract.interactions.models.formats import AnswerFormat, AnswerMismatchPolicy
from tai42_contract.interactions.models.forms import FormData, FormPage, check_form_data, check_form_pages
from tai42_contract.interactions.models.media import MediaItem, check_media_list
from tai42_contract.states import StateContext

# Cap on a per-ask custom mismatch notice — the participant-facing rejection text a ``retry``-policy ask
# may substitute for the built-in one. A single participant reply, so a small bound (channels impose
# their own tighter message caps); matches the conversation route's ``error_reply_text`` bound.
MISMATCH_NOTICE_MAX_CHARS = 2000

# Cap on the question text stored verbatim into the interaction state hash and the
# per-group stream — a prompt authored by a server tool, so a few KB is generous.
# Bounds the durable record and its replay, a wire-contract property, so it is a
# constant, never a setting; an over-cap question is refused loudly.
QUESTION_MAX_CHARS = 8192


def _check_select_payload(payload: dict[str, Any]) -> None:
    options = payload.get("options")
    if not options:
        raise ValueError("select answer_format requires non-empty options")
    # SELECT carries only its answer set; form-only keys (data/pages) and any
    # other extra are a caller bug, refused rather than silently ignored.
    extra = set(payload) - {"options"}
    if extra:
        raise ValueError(f"select answer_format payload carries only options, got extra {sorted(extra)}")


def _check_form_payload(payload: dict[str, Any]) -> None:
    schema = payload.get("schema")
    if not schema:
        raise ValueError("form answer_format requires a schema")
    # Per-send prefill/options and stepped pages are validated ONCE here, the
    # single seam every ask door flows through, against the form's own schema.
    data = payload.get("data")
    pages = payload.get("pages")
    if (data is not None or pages is not None) and not isinstance(schema, dict):
        raise ValueError("form data/pages require an object schema")
    if data is not None:
        check_form_data(cast("dict[str, Any]", schema), FormData.model_validate(data))
    if pages is not None:
        check_form_pages(
            cast("dict[str, Any]", schema), [FormPage.model_validate(page) for page in cast("list[Any]", pages)]
        )


def _check_external_payload(payload: dict[str, Any]) -> None:
    url = payload.get("url")
    # The external surface is reached through this url; a non-str or empty
    # value would leave the caller with no place to send the human.
    if not isinstance(url, str) or not url:
        raise ValueError("external answer_format requires a non-empty string url in format_payload")


def _check_text_payload(payload: dict[str, Any] | None) -> None:
    # TEXT carries no payload EXCEPT an OPTIONAL ``options`` list of suggested
    # replies: a tapped option submits its own text as the free-text answer, which
    # stays unconstrained (unlike SELECT, where options ARE the answer set). Any
    # other key on a text payload is a caller bug.
    if payload is not None:
        extra = set(payload) - {"options"}
        if extra:
            raise ValueError(f"text answer_format payload carries only optional options, got extra {sorted(extra)}")
        options = payload.get("options")
        if options is not None and not options:
            raise ValueError("text answer_format options must be a non-empty list when present")


def _check_no_payload(answer_format: AnswerFormat, payload: dict[str, Any] | None) -> None:
    if payload is not None:
        raise ValueError(f"{answer_format.value} answer_format carries no format_payload")


class InteractionRequest(BaseModel):
    """The durable question. One per stream entry."""

    interaction_id: str
    group_id: str
    question: str
    answer_format: AnswerFormat = AnswerFormat.TEXT
    format_payload: dict[str, Any] | None = None
    # What a channel-delivered ask does with a participant reply the answer door REJECTS: ``retry``
    # (default — keep the ask parked and tell the participant what's expected) or ``bridge`` (treat an
    # unmatched reply as a digression — keep the ask parked with no notice and hand the reply to
    # the conversation as a fresh routed turn). Set per ask by the tool author; the default is a
    # zero-behavior-change for every existing ask.
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY
    # A per-ask custom participant-facing rejection notice, used ONLY under the ``retry`` policy: when
    # set it REPLACES the platform's built-in retry notice. A literal ``{reason}`` token (if
    # present) is filled with the door's rejection reason by a PLAIN substitution — a notice
    # without the token is sent verbatim, and stray braces never raise (never ``str.format``). It
    # customizes the CORE-sent notice only; a channel that owns its correction surface renders its
    # own text off the door's reason and ignores this. Under the ``bridge`` policy it is IGNORED (a
    # digression never notifies). ``None`` uses the built-in default.
    mismatch_notice: str | None = None
    reply_to: str
    created_at: datetime
    timeout_at: datetime
    # When set, the answer body is treated as sensitive (credentials, personal
    # data) and is never persisted into the answered state — the blocked caller
    # still receives the full answer through the reply channel, but the durable
    # record keeps only the answered status. Set per question by the tool author.
    sensitive: bool = False
    # Name of the registered channel that delivered this question out-of-band;
    # None means the question surfaced in the inbox only. Set by the asking
    # side when the question is raised with a channel, so consumers can
    # attribute the medium the answer arrived through.
    channel: str | None = None
    # The channel delivery address the asking side passed for this question (a
    # chat id, phone number, ...) — display/binding attribution on the operator
    # feed, a WHERE, never an authorization axis. None when no address was
    # passed; a set value is a non-blank string (the helper rejects blanks).
    recipient: str | None = None
    # The run/thread that raised this question — the background tool-run id
    # stamped when the question is asked inside a tool run, None outside one. It
    # attributes a pending question and lets programmatic answering bind it to
    # the originating run. A set value is a non-blank string.
    origin: str | None = None
    # Identity (a user_id) the interaction is scoped to: a restricted caller sees
    # and answers only questions addressed to its own identity. This is the
    # isolation axis, distinct from ``channel``/``reply_to`` delivery addressing
    # — it is a who, not a where. None means the question is unaddressed
    # (an operator/broadcast question every unrestricted caller may see).
    audience: str | None = None
    # Display-only media shown WITH the question: images and links the human sees when
    # reading it. It never becomes part of the answer. It renders in the inbox AND, on a
    # channel-delivered ask, is forwarded on ``ChannelDelivery.media`` to the channel plugin
    # (a data:image served reference is absolute on that path so a vendor can fetch it
    # off-origin). None means no media; a present list is non-empty (a present-but-empty list
    # is a caller bug). Set per question by the tool author.
    media: list[MediaItem] | None = None
    # Wait discipline. ``sync`` blocks the asking caller until the answer or the
    # timeout; ``async`` PARKS the caller — the ask returns a SuspendedInteraction
    # and a later answer/expiry resumes work by invoking ``continuation_tool`` as
    # ``continuation_identity``. Both continuation fields are required iff async
    # and forbidden iff sync.
    mode: Literal["sync", "async"] = "sync"
    # async only: the registered tool NAME run when the answer arrives or the
    # question expires — resolved through the platform tool registry, carrying no
    # resuming-driver state.
    continuation_tool: str | None = None
    # async only: the execution key (an api-key ``user_id``) the continuation runs
    # AS — rebound at answer time, never the answerer's identity. Same string
    # representation as ``execution_key`` elsewhere in the contract.
    continuation_identity: str | None = None
    # The ambient state context the ORIGINAL turn deposited, carried verbatim across
    # the park so the resumed run's state writes complete their provenance from the
    # same door — one generic snapshot (a later resume attribution joins the same
    # field). None when the park ran under no state context.
    continuation_state_context: StateContext | None = None
    # When the parked question expires. Distinct from ``timeout_at`` (the sync
    # wait budget) and mutually exclusive with a sync ``timeout`` at the ask
    # surface (see ``check_ask_timing``). Required for async (a park always carries
    # a deadline; the model validator rejects an async request without it); None
    # only on a sync question.
    expiry_at: datetime | None = None

    @field_validator("question")
    @classmethod
    def _check_question(cls, value: str) -> str:
        if len(value) > QUESTION_MAX_CHARS:
            raise ValueError(f"question must be at most {QUESTION_MAX_CHARS} characters, got {len(value)}")
        return value

    @field_validator("mismatch_notice")
    @classmethod
    def _check_mismatch_notice(cls, value: str | None) -> str | None:
        # None uses the built-in default; a set notice is non-blank and within the participant-reply cap.
        if value is not None:
            if not value.strip():
                raise ValueError("mismatch_notice must be non-blank when set")
            if len(value) > MISMATCH_NOTICE_MAX_CHARS:
                raise ValueError(
                    f"mismatch_notice must be at most {MISMATCH_NOTICE_MAX_CHARS} characters, got {len(value)}"
                )
        return value

    @field_validator("media")
    @classmethod
    def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        if value is not None:
            check_media_list(value)
        return value

    @field_validator("created_at", "timeout_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # A naive timeout_at compared against an aware ``now()`` raises TypeError
        # at use time; reject it here and normalize to UTC (same strictness as
        # ConnectionRecord).
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @field_validator("expiry_at")
    @classmethod
    def _ensure_expiry_tz_aware(cls, value: datetime | None) -> datetime | None:
        # The async park deadline is compared against an aware ``now()`` by the
        # expiry reaper; a naive value would raise TypeError there. None stays None
        # (a sync question carries no deadline); a set value is reject-naive +
        # UTC-normalized, the same strictness as its ``created_at``/``timeout_at``
        # siblings.
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_payload(self) -> InteractionRequest:
        if self.answer_format is AnswerFormat.SELECT:
            _check_select_payload(self.format_payload or {})
        elif self.answer_format is AnswerFormat.FORM:
            _check_form_payload(self.format_payload or {})
        elif self.answer_format is AnswerFormat.EXTERNAL:
            _check_external_payload(self.format_payload or {})
        elif self.answer_format is AnswerFormat.TEXT:
            _check_text_payload(self.format_payload)
        else:
            _check_no_payload(self.answer_format, self.format_payload)
        return self

    @model_validator(mode="after")
    def _check_continuation(self) -> InteractionRequest:
        if self.mode == "async":
            if self.continuation_tool is None:
                raise ValueError("async mode requires continuation_tool")
            if self.continuation_identity is None:
                raise ValueError("async mode requires continuation_identity")
            if self.expiry_at is None:
                # Without an ``expiry_at`` an async park is never expiry-indexed, so
                # the reaper can never fire its continuation — the idle TTL would drop
                # it silently. Require the deadline rather than persist an
                # unresumable park.
                raise ValueError("async mode requires expiry_at")
        else:
            if self.continuation_tool is not None:
                raise ValueError("sync mode carries no continuation_tool")
            if self.continuation_identity is not None:
                raise ValueError("sync mode carries no continuation_identity")
        return self
