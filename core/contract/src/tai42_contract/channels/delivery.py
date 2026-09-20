"""``ChannelDelivery`` — one question handed to a channel for out-of-band delivery."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tai42_contract.interactions.models import (
    MISMATCH_NOTICE_MAX_CHARS,
    AnswerFormat,
    AnswerMismatchPolicy,
    FormData,
    FormPage,
    MediaItem,
    check_media_list,
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
        pages: list[FormPage] | None = None
        media: list[MediaItem] | None = None  # display media rendered WITH the question; None -> none
        # The ask's digression policy for a rejected reply; the channel carries it onto the
        # ``Correlation`` it parks so the shared answer ladder reads it at the 400 decision.
        on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY
        # The ask's custom retry-notice text (``retry`` policy only), carried onto the parked
        # ``Correlation`` for the ladder; ``None`` uses the built-in notice.
        mismatch_notice: str | None = None
        callback_url: str  # public /api/interactions/callback/{ticket} — the answer sink
        timeout_at: datetime  # tz-aware; the plugin may surface a deadline to the human

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

        @field_validator("mismatch_notice")
        @classmethod
        def _check_mismatch_notice(cls, value: str | None) -> str | None:
            # None uses the built-in default; a set notice is re-validated to the SAME
            # non-blank + participant-reply cap the ask REQUEST (``InteractionRequest``) enforces,
            # so the delivery frame is bounded exactly as the ask that produced it — the
            # symmetric defensive re-check the ``media`` re-validation above applies.
            if value is not None:
                if not value.strip():
                    raise ValueError("mismatch_notice must be non-blank when set")
                if len(value) > MISMATCH_NOTICE_MAX_CHARS:
                    raise ValueError(
                        f"mismatch_notice must be at most {MISMATCH_NOTICE_MAX_CHARS} characters, got {len(value)}"
                    )
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
            # ``data``/``pages`` ride ONLY a form delivery — they enrich the form's answer
            # schema, which no other format carries. Present on any other format is a caller
            # bug, refused loudly rather than silently ignored.
            if self.answer_format != AnswerFormat.FORM:
                if self.data is not None:
                    raise ValueError(f"{self.answer_format} answer_format carries no form data")
                if self.pages is not None:
                    raise ValueError(f"{self.answer_format} answer_format carries no form pages")
            return self
