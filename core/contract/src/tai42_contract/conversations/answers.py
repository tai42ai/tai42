"""The turn outcome: an ordered ``AnswerPart`` list, its joined text, and ``ConversationAnswer``."""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.channels import (
    NOTIFICATION_MESSAGE_MAX_CHARS,
    ChannelTemplate,
    Option,
    OptionSection,
    check_footer,
    check_header,
    check_interactive_composition,
    check_options,
    check_sections,
)
from tai42_contract.interactions.models import (
    FormData,
    FormPage,
    LocationElement,
    MediaItem,
    check_form_data,
    check_form_pages,
    check_media_list,
)

#: A turn's outcome kind. ``answered``/``error`` carry answer text (``error`` is generic
#: client-safe text only); ``silent`` is a deliberate no-reply and carries NO answer text.
AnswerStatus = Literal["answered", "error", "silent"]


with warnings.catch_warnings():
    # The ``schema`` field intentionally shadows pydantic's deprecated
    # ``BaseModel.schema()`` alias (the current API is ``model_json_schema()``);
    # the field name matches the JSON-schema payload it carries. Suppress the
    # shadow warning at the definition site so every importer is safe regardless
    # of its own warnings config — narrowly matched, never a blanket ignore.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class AnswerPart(BaseModel):
        """One message of an ordered multi-message answer — the platform's rich part shape.

        It mirrors :class:`ChannelNotification`'s CONTENT surface exactly (``message`` plus the
        optional ``media`` / ``location`` / ``template`` / ``options`` / ``sections`` / ``header``
        / ``footer`` / ``schema`` richer-send forms and their validators), MINUS the per-delivery
        routing fields (``recipient`` / ``sender_identity``) — those stay on the single delivery,
        never per part. The delivery machine sends each part as its own ``ChannelNotification``,
        in order, chunking the ``message`` text at the channel width and carrying the part's
        richer forms alongside it, exactly as a single notification does today. Every
        cross-field rule (content-only blank message, ``options`` XOR ``sections``, ``schema``
        excludes both, ``header``/``footer`` require a choice surface, ``template`` standalone) is
        the SHARED :func:`~tai42_contract.channels.check_interactive_composition`, so an authored
        part and a delivered notification can never diverge.

        A part is a NEW authoring surface, so it is STRICT FROM BIRTH: unknown keys are refused
        (``extra="forbid"``) rather than silently dropped. A tool route authors parts as a JSON
        array whose elements are each EITHER a plain string (shorthand for a text-only part) or a
        part object — both normalize to THIS one model. Frozen.

        ``message`` mirrors :class:`ChannelNotification.message`'s blank-vs-media RULE: non-blank BY
        DEFAULT, EXCEPT it may be blank for a MEDIA-ONLY part — a caption-less image with no text
        carrier. The admissible states are "``message`` non-blank" OR "blank ``message`` WITH non-empty
        ``media``"; a blank ``message`` and no media has nothing to deliver and is refused. ``options``
        REQUIRE a non-blank ``message`` (a tappable choice needs a prompt), and a ``template`` rides a
        non-blank ``message`` too — so a media-only part carries only ``media``. Unlike
        ``ChannelNotification`` (always constructed in code), a part is authored as JSON where the
        ``message`` key may be ABSENT, so it defaults to ``""`` — a media-only part is just ``{"media": …}``.
        """

        model_config = ConfigDict(frozen=True, extra="forbid")

        message: str = ""  # human-readable text; blank/omitted ONLY for a content-only part (media/location carries it)
        media: list[MediaItem] | None = None  # display media sent WITH the message; None -> none
        location: LocationElement | None = None  # a shared geographic point; None -> none
        template: ChannelTemplate | None = None  # out-of-window template send; None -> freeform
        options: list[Option] | None = None  # flat tappable options (reply/link); None -> none
        sections: list[OptionSection] | None = None  # a sectioned option list; None -> none
        header: MediaItem | None = None  # single media header above an interactive message; None -> none
        footer: str | None = None  # short trailing line under an interactive message; None -> none
        # The form answer schema for an ask-less form; the submission enters the conversation as
        # a participant message. Intentionally named ``schema`` (matches the payload it carries);
        # shadows the deprecated ``BaseModel.schema()`` alias, which this model never uses.
        schema: dict[str, Any] | None = None  # pyright: ignore[reportIncompatibleMethodOverride]
        # Per-send prefill/options layered over ``schema`` for THIS part's form, so a reply
        # part can open its form already filled in; ride ONLY a form part (``schema`` present).
        data: FormData | None = None
        # Stepped-form layout over ``schema``: each :class:`FormPage` names the top-level
        # properties on one step; ride ONLY a form part. Absent ``pages`` means one page.
        pages: list[FormPage] | None = None

        @field_validator("message")
        @classmethod
        def _message_valid(cls, value: str) -> str:
            # Mirrors ChannelNotification.message: length cap only here, with the blank-vs-media
            # rule decided in :meth:`_message_or_media` once every field is bound.
            if len(value) > NOTIFICATION_MESSAGE_MAX_CHARS:
                raise ValueError(
                    f"message must be at most {NOTIFICATION_MESSAGE_MAX_CHARS} characters, got {len(value)}"
                )
            return value

        @field_validator("media")
        @classmethod
        def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
            if value is not None:
                check_media_list(value)
            return value

        @field_validator("options")
        @classmethod
        def _options_valid(cls, value: list[Option] | None) -> list[Option] | None:
            return check_options(value)

        @field_validator("sections")
        @classmethod
        def _sections_valid(cls, value: list[OptionSection] | None) -> list[OptionSection] | None:
            return check_sections(value)

        @field_validator("footer")
        @classmethod
        def _footer_valid(cls, value: str | None) -> str | None:
            return check_footer(value)

        @field_validator("header")
        @classmethod
        def _header_valid(cls, value: MediaItem | None) -> MediaItem | None:
            return check_header(value)

        @field_validator("schema")
        @classmethod
        def _schema_non_empty(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
            # Mirrors ChannelNotification: None means no form; a present schema is a non-empty
            # dict. The deep shape is the sender's shared channel-deliverable subset walk, never
            # re-implemented here.
            if value is not None and not value:
                raise ValueError("schema must be a non-empty dict when present")
            return value

        @model_validator(mode="after")
        def _check_composition(self) -> AnswerPart:
            # The SAME shared cross-field rules ChannelNotification enforces, so the flow-message
            # authoring surface and the delivery frame can never drift.
            check_interactive_composition(
                message=self.message,
                media=self.media,
                location=self.location,
                template=self.template,
                options=self.options,
                sections=self.sections,
                schema=self.schema,
                header=self.header,
                footer=self.footer,
                noun="part",
            )
            return self

        @model_validator(mode="after")
        def _check_form_extras(self) -> AnswerPart:
            # ``data``/``pages`` enrich a form part's ``schema`` (prefill, per-send option
            # lists, stepped pages), so they ride ONLY a part that carries a ``schema`` — on
            # any other part they name a form that is not there and are refused loudly. When a
            # form part carries them, each is cross-checked against THIS part's ``schema`` (the
            # same :func:`check_form_data`/:func:`check_form_pages` the ask path's
            # ``InteractionRequest`` runs), so a bad prefill is refused here rather than
            # delivered as a partly filled form.
            if self.schema is None:
                if self.data is not None:
                    raise ValueError("data rides a form part (a part with a schema) only")
                if self.pages is not None:
                    raise ValueError("pages ride a form part (a part with a schema) only")
                return self
            if self.data is not None:
                check_form_data(self.schema, self.data)
            if self.pages is not None:
                check_form_pages(self.schema, self.pages)
            return self

        def is_plain_text(self) -> bool:
            """Whether this part carries nothing beyond its ``message`` — the case a
            single-message answer degenerates to (``parts`` then adds nothing over the joined
            ``answer`` and is dropped). A part carrying media, a location, a template, options,
            sections, a header, a footer or a schema is NOT plain text — it adds what the joined
            ``answer`` cannot carry."""
            return (
                self.media is None
                and self.location is None
                and self.template is None
                and self.options is None
                and self.sections is None
                and self.header is None
                and self.footer is None
                and self.schema is None
            )


def joined_answer_text(parts: Sequence[AnswerPart]) -> str:
    """The whole-text form of an ordered ``parts`` list: the NON-BLANK part messages joined
    with a blank line — the single string every legacy reader (api-door callbacks, sync waits,
    transcripts) consumes. A MEDIA-ONLY part (blank message) contributes NOTHING to the text,
    so an all-media answer joins to the empty string; the media itself rides ``parts``, which a
    parts-aware consumer delivers. The one definition both the wire :class:`ConversationAnswer`
    and the host record share, so the joined text can never diverge between them."""
    return "\n\n".join(part.message for part in parts if part.message.strip())


class ConversationAnswer(BaseModel):
    """The outcome of one conversation turn — the body POSTed (HMAC-signed) to a
    ``door=api`` row's ``callback_url`` AND the bounded sync-wait payload.

    ``message_id`` correlates it to the ``202``/``200`` the door returned. On
    ``status="error"`` the ``answer`` is generic client-safe text, never an internal
    detail. On ``status="silent"`` the turn produced no reply and ``answer`` is absent.

    ``parts`` is the ordered list of :class:`AnswerPart` messages the turn produced when a
    single joined string would lose something — more than one message, or one message
    carrying media/options/a template. It is order-significant; when it is present ``answer``
    equals the parts' NON-BLANK MESSAGE texts joined with ``"\n\n"`` (:func:`joined_answer_text`),
    so every consumer of ``answer`` (api-door callbacks, sync waits, transcripts) keeps seeing
    the whole text with zero migration while a parts-aware consumer reads ``parts`` and delivers
    each as its own message (with its media/options). A single PLAIN-TEXT answer carries
    ``parts=None`` (the joined ``answer`` says everything); a richer or multi-message answer
    carries the parts. An ALL-MEDIA answer (every part media-only) joins to the EMPTY string, so
    on ``answered``/``error`` a blank ``answer`` is admissible ONLY when ``parts`` carry the
    content — the media rides ``parts``. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: AnswerStatus
    answer: str | None = None
    parts: list[AnswerPart] | None = None

    @model_validator(mode="after")
    def _answer_matches_status(self) -> ConversationAnswer:
        """``answered``/``error`` carry answer text (a string, possibly EMPTY for an all-media
        answer whose ``parts`` carry the content); ``silent`` carries none. A blank ``answer`` is
        admissible on ``answered``/``error`` ONLY when ``parts`` is present — otherwise there is
        nothing to deliver."""
        if self.status == "silent":
            if self.answer is not None:
                raise ValueError("a silent answer carries no answer text")
            return self
        if self.answer is None:
            raise ValueError("an answered/error answer carries answer text (empty only for an all-media answer)")
        if not self.answer.strip() and not self.parts:
            raise ValueError("an answered/error answer with blank text must carry media-only parts")
        return self

    @model_validator(mode="after")
    def _parts_mirror_the_answer(self) -> ConversationAnswer:
        """A present ``parts`` list is non-empty, rides an ``answered``/``error`` outcome
        (never ``silent``), and its NON-BLANK part MESSAGE texts join with ``"\n\n"`` to exactly
        ``answer`` (a media-only part contributes nothing) — so the joined text a legacy consumer
        reads and the ordered parts a parts-aware consumer delivers can never disagree. Each
        part's own shape (message-or-media, media/options/template exclusivity) is enforced by
        :class:`AnswerPart`."""
        if self.parts is None:
            return self
        if not self.parts:
            raise ValueError("parts must be a non-empty list when present")
        if self.status == "silent":
            raise ValueError("a silent answer carries no parts")
        if joined_answer_text(self.parts) != (self.answer or ""):
            raise ValueError("answer must equal the non-blank part messages joined with a blank line")
        return self
