"""Client-facing inbound bodies: a message, a structured event, and its submission envelope."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.conversations.inbound_form import validate_bounded_object, validate_inbound_form
from tai42_contract.entry_params import validate_entry_params
from tai42_contract.interactions.models import LocationElement, MediaItem, check_media_list
from tai42_contract.locale import normalize_optional_locale


class BlankInboundTextError(ValueError):
    """The channel door was handed a blank/whitespace-only message body — nothing to run
    a turn on. Raised by ``AppConversations.accept``; a channel adapter catches it and
    drops the inbound (log + ack), exactly as it drops an unrouted one."""


class ConversationMessage(BaseModel):
    """The client-facing inbound body of the authed API door
    ``POST /api/conversations/{route_name}/messages``.

    ``external_user_id`` is the caller's handle for the end user: it becomes the
    ``client_address`` the answer is delivered against and the conversation's thread
    key. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    external_user_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    wait_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Bounded sync-wait window (seconds); absent = async 202. The door clamps to its runtime cap.",
    )
    params: dict[str, str] | None = Field(
        default=None,
        description=(
            "Opaque caller-supplied entry parameters delivered to a tool target's payload "
            "under ``params``; the platform attaches no meaning and no trust."
        ),
    )
    form: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A structured participant submission (an ask-less form's answers) riding WITH the "
            "text. ``text`` stays required and non-blank — it is the CARRIER every reader "
            "consumes: a channel submits a faithful text form of the submission alongside "
            "the structured data, so a form-unaware consumer still sees the whole turn, "
            "and the ``attachments``/``location`` siblings ride the same pattern. The "
            "platform attaches no meaning and NO TRUST to the contents: participant-shaped "
            "data, never schema-conformant — a target that reads it validates it itself."
        ),
    )
    attachments: list[MediaItem] | None = Field(
        default=None,
        description=(
            "Structured media the caller sent WITH the text (image/document/video/audio) — the "
            "inbound counterpart of an outbound answer's media. Delivered to a tool target's "
            "payload under ``attachments`` only when present; the ``text`` stays the whole turn "
            "every reader consumes."
        ),
    )
    location: LocationElement | None = Field(
        default=None,
        description=(
            "A geographic point the caller shared WITH the text. Delivered to a tool target's "
            "payload under ``location`` only when present; the ``text`` stays the whole turn "
            "every reader consumes."
        ),
    )
    locale: str | None = Field(
        default=None,
        description=(
            "The end user's BCP 47 language tag (e.g. ``he-IL``). Captured onto the turn's "
            "subject so the platform's rendering layer resolves every template and list format "
            "against it — the caller states the language, the flow never selects one. ``null`` "
            "means none supplied (no silent default)."
        ),
    )

    @field_validator("external_user_id", "text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-blank")
        return value

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return validate_entry_params(value)

    @field_validator("form")
    @classmethod
    def _check_form(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # None means no structured submission; a present dict is bounded as pure transport
        # (shape, depth, size) — its meaning stays opaque and untrusted.
        if value is None:
            return None
        return validate_inbound_form(value)

    @field_validator("attachments")
    @classmethod
    def _check_attachments(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        # None means no inbound media; a present list carries the same list-level caps (non-empty,
        # item count, summed URI) every media door shares — each item's own shape is MediaItem's.
        if value is not None:
            check_media_list(value)
        return value

    @field_validator("locale")
    @classmethod
    def _canonical_locale(cls, value: str | None) -> str | None:
        return normalize_optional_locale(value)


# An event ``kind`` is a namespaced identifier-like label (e.g. ``provider.update``): the
# dot/colon segments the module's ``:``-free slug patterns do not admit, capped at 128 chars.
EVENT_KIND_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
#: The idempotency key length cap, measured after trimming surrounding whitespace.
EVENT_ID_MAX_CHARS = 256


class ConversationEvent(BaseModel):
    """A structured event delivered to an existing thread as a turn.

    ``event_id`` (non-blank after trim, ≤ ``EVENT_ID_MAX_CHARS``) is the idempotency key;
    ``kind`` is an identifier-like label matching ``EVENT_KIND_RE``; ``payload`` is opaque,
    untrusted data bounded as pure transport by :func:`validate_bounded_object`. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def _check_event_id(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("event_id must be non-blank")
        if len(trimmed) > EVENT_ID_MAX_CHARS:
            raise ValueError(f"event_id must be at most {EVENT_ID_MAX_CHARS} characters after trimming")
        return value

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if not EVENT_KIND_RE.fullmatch(value):
            raise ValueError(f"kind must match {EVENT_KIND_RE.pattern!r}")
        return value

    @field_validator("payload")
    @classmethod
    def _check_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_bounded_object(value, what="event payload")


class ConversationEventSubmission(BaseModel):
    """The inbound body of the event door ``POST /api/conversations/{route_name}/events``.

    An :class:`ConversationEvent` addressed to an EXISTING thread by EXACTLY ONE (non-blank)
    of ``address`` (the thread's client address) or ``thread_id`` (the id the monitoring
    listing exposes). ``wait_seconds`` bounds a sync-wait window exactly as
    :attr:`ConversationMessage.wait_seconds`. There is NO callback field — an event's answer
    is delivered against the target thread's route. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str | None = None
    thread_id: str | None = None
    event: ConversationEvent
    wait_seconds: int = Field(
        default=0,
        ge=0,
        description="Bounded sync-wait window (seconds); 0 = async 202. The door clamps to its runtime cap.",
    )

    @model_validator(mode="after")
    def _exactly_one_thread_ref(self) -> ConversationEventSubmission:
        # A present-but-blank reference is malformed, never "absent": the door branches on
        # presence, so blank and None must not be admitted as the same thing.
        for name in ("address", "thread_id"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be non-blank when given")
        if (self.address is None) == (self.thread_id is None):
            raise ValueError("exactly one of address or thread_id is required")
        return self
