"""``ChannelDelivery`` — one question handed to a channel — and what a channel send reports.

``SendOutcome`` is what a completed ``notify`` or ``deliver`` reports about itself;
``ChannelAddress`` is where a channel would send before it sends.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tai42_contract.channels.notification import NOTIFICATION_ADDRESS_MAX_CHARS
from tai42_contract.interactions.models import (
    AnswerFormat,
    AnswerMismatchPolicy,
    FormData,
    FormPage,
    FormReactions,
    MediaItem,
    Wording,
    check_media_list,
    check_wording_for_ask,
)

with warnings.catch_warnings():
    # The ``schema`` field intentionally shadows pydantic's deprecated
    # ``BaseModel.schema()`` alias (the current API is ``model_json_schema()``);
    # the field name matches the JSON-schema payload it carries. Suppress the
    # shadow warning at the definition site so every importer is safe regardless
    # of its own warnings config — narrowly matched, never a blanket ignore.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class ChannelDelivery(BaseModel):
        """One question handed to a channel for out-of-band delivery.

        ``callback_url`` is the public ``/api/interactions/callback/{ticket}``
        answer sink; the channel arranges for the human's reply to reach it.
        ``recipient`` is the OPTIONAL caller-requested address (chat id, phone
        number, ...): the channel plugin validates it against its operator-set
        allowlist and refuses to send to an unlisted address; when omitted the
        plugin sends to its operator-configured default recipient. It is an
        address only, never a secret or credential.

        ``media`` is OPTIONAL display media the channel renders alongside the
        question (reusing :class:`MediaItem`, the same shape and list-level caps the
        ask REQUEST carries); a present list is non-empty. It is a pure enhancement,
        NOT structure: a channel that renders only text simply ignores it and shows
        the question, so it rides no capability flag and is never refused for a
        channel that cannot render it. ``options`` is REQUIRED for ``select`` (the
        answer set) and OPTIONAL for ``text`` as SUGGESTED REPLIES — a tapped option
        submits its own text as the free-text answer; every other format carries none.
        """

        model_config = ConfigDict(frozen=True)

        interaction_id: str
        recipient: str | None = None  # caller-requested address; None -> plugin default
        question: str
        answer_format: str  # channel-delivered set: "text" | "confirm" | "select" | "form" | "external"
        # The form's JSON answer schema; present exactly when answer_format == "form".
        # Intentionally named ``schema`` (matches the payload it carries); shadows the
        # deprecated ``BaseModel.schema()`` alias, which this model never uses.
        schema: dict[str, Any] | None = None  # pyright: ignore[reportIncompatibleMethodOverride]
        options: list[str] | None = None  # required for select; optional suggested replies for text
        # Per-send form enrichment, present only for ``form``: known ``values`` shown filled
        # in and per-send ``options`` (a choice list REPLACING a property's enum for this
        # send). None means the ask carried none. The values/options were validated against
        # the answer schema at the ask door; a channel renders them into its own form surface.
        data: FormData | None = None
        # The form's step layout, present only for ``form``: each :class:`FormPage` names the
        # top-level properties one step collects. None means one page (the whole form). A
        # channel with native steps renders one step per page; a channel without them may
        # render the pages as titled groups on one surface; the completed answer is the union.
        # Each :class:`FormPage` also carries its ordered display blocks and its input/review
        # kind, so the display layout rides this field unchanged.
        pages: list[FormPage] | None = None
        # The form's reaction triggers, present only for ``form``: which fields' change,
        # which pages' advance, and whether submission fires the form's reaction handler
        # while it is open. None means a static (non-reacting) form. Validated against the
        # schema/pages at the ask door; a channel renders the reactive controls and routes
        # each trigger back through the reaction door.
        reactions: FormReactions | None = None
        # The caller's words for this send's platform-drawn texts (buttons, headings, message
        # lines, answer-page sentences, refusal sentences, status copy), grouped by what each
        # decorates (:class:`~tai42_contract.interactions.models.wording.Wording`). Each member is
        # admitted only on a send that draws it, re-checked against ``answer_format`` here as the
        # ask REQUEST checked it, so the delivery frame is bounded exactly as the ask that produced
        # it. None -> each surface shows its own word.
        wording: Wording | None = None
        media: list[MediaItem] | None = None  # display media rendered WITH the question; None -> none
        # The ask's digression policy for a rejected reply; the channel carries it onto the
        # ``Correlation`` it parks so the shared answer ladder reads it at the 400 decision.
        on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY
        callback_url: str  # public /api/interactions/callback/{ticket} — the answer sink
        timeout_at: datetime  # tz-aware; the plugin may surface a deadline to the human
        # True when the answer body must not be persisted. A channel that stores what passed
        # through it keeps that an answer was given, never the answer.
        sensitive: bool

        @field_validator("recipient")
        @classmethod
        def _recipient_non_empty(cls, value: str | None) -> str | None:
            if value is not None and not value.strip():
                raise ValueError("recipient must be a non-empty address when present")
            return value

        @field_validator("answer_format")
        @classmethod
        def _channel_deliverable_format(cls, value: str) -> str:
            # Every AnswerFormat is channel-deliverable — "form" behind the
            # channel's ``supports_form_delivery`` capability flag; only an
            # unknown value is rejected.
            if value not in AnswerFormat:
                raise ValueError(f"answer_format must be one of {sorted(f.value for f in AnswerFormat)}, got {value!r}")
            return value

        @field_validator("timeout_at")
        @classmethod
        def _ensure_tz_aware(cls, value: datetime) -> datetime:
            # A naive timeout_at compared against an aware ``now()`` raises TypeError
            # at use time; reject it here and normalize to UTC (same strictness as
            # InteractionRequest).
            if value.tzinfo is None:
                raise ValueError("timeout_at must be timezone-aware (UTC)")
            return value.astimezone(UTC)

        @field_validator("media")
        @classmethod
        def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
            # None means no media; a present list carries the same list-level caps
            # (non-empty, item count, summed URI) the ask REQUEST enforces — media is the
            # question's display enhancement the channel renders alongside it, so the
            # delivery frame is bounded exactly as the ask that produced it. A channel that
            # cannot render media ignores it (no capability flag), never refuses the send.
            if value is not None:
                check_media_list(value)
            return value

        @model_validator(mode="after")
        def _check_options(self) -> ChannelDelivery:
            # SELECT REQUIRES options — the answer set the human chooses from. TEXT MAY
            # carry options as SUGGESTED REPLIES: a tapped option submits its own text as
            # the free-text answer (text accepts any string), so they are an optional
            # enhancement, never a constraint. Every other format carries none.
            if self.answer_format == AnswerFormat.SELECT:
                if not self.options:
                    raise ValueError("select answer_format requires non-empty options")
            elif self.answer_format == AnswerFormat.TEXT:
                if self.options is not None and not self.options:
                    raise ValueError("text answer_format options must be a non-empty list when present")
            elif self.options is not None:
                raise ValueError(f"{self.answer_format} answer_format carries no options")
            return self

        @model_validator(mode="after")
        def _check_schema(self) -> ChannelDelivery:
            if self.answer_format == AnswerFormat.FORM:
                if not self.schema:
                    raise ValueError("form answer_format requires a non-empty schema")
            elif self.schema is not None:
                raise ValueError(f"{self.answer_format} answer_format carries no schema")
            return self

        @model_validator(mode="after")
        def _check_form_extras(self) -> ChannelDelivery:
            # ``data``/``pages``/``reactions`` ride ONLY a form delivery — they enrich the form's
            # answer schema, which no other format carries. Present on any other format is a caller
            # bug, refused loudly rather than silently ignored.
            if self.answer_format != AnswerFormat.FORM:
                if self.data is not None:
                    raise ValueError(f"{self.answer_format} answer_format carries no form data")
                if self.pages is not None:
                    raise ValueError(f"{self.answer_format} answer_format carries no form pages")
                if self.reactions is not None:
                    raise ValueError(f"{self.answer_format} answer_format carries no form reactions")
            return self

        @model_validator(mode="after")
        def _check_wording(self) -> ChannelDelivery:
            # Each ``wording`` member is admitted only on a send the format draws its control or
            # sentence on — the SAME admission table the ask REQUEST (``InteractionRequest``) reads,
            # so the delivery frame is bounded exactly as the ask that produced it. A member on a
            # format that draws nothing for it is refused loudly rather than silently dropped.
            if self.wording is not None:
                check_wording_for_ask(
                    self.wording,
                    self.answer_format,
                    has_options=bool(self.options),
                    has_pages=self.pages is not None,
                )
            return self


def _check_address(value: str) -> str:
    if not value.strip():
        raise ValueError("must be a non-empty address")
    if len(value) > NOTIFICATION_ADDRESS_MAX_CHARS:
        raise ValueError(f"address must be at most {NOTIFICATION_ADDRESS_MAX_CHARS} characters, got {len(value)}")
    return value


class ChannelAddress(BaseModel):
    """The identity and participant address a channel would send to, known before sending.

    ``our_identity`` is the identity the send goes from; a completed send's
    :class:`SendOutcome` reports the same identity. ``address`` may not be the canonical
    one: a medium that resolves a handle only when it sends gives the handle here.
    """

    model_config = ConfigDict(frozen=True)

    our_identity: str
    address: str

    @field_validator("our_identity", "address")
    @classmethod
    def _bounded(cls, value: str) -> str:
        return _check_address(value)


class SendOutcome(BaseModel):
    """What a completed channel send reports about itself.

    ``our_identity`` is the identity the message went out from. ``address`` is the
    participant address the medium delivered to, in the same form the channel's inbound
    door passes to ``accept`` as ``client_address`` (taken from the send response where
    the medium reveals that form only there). ``message_ids`` are the provider ids of the
    messages sent, in send order (several when the medium splits a long message, empty
    when it exposes no id).
    """

    model_config = ConfigDict(frozen=True)

    our_identity: str
    address: str
    message_ids: list[str]

    @field_validator("our_identity", "address")
    @classmethod
    def _bounded(cls, value: str) -> str:
        return _check_address(value)
