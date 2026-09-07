"""Conversation bridge contract types.

The messaging bridge turns an inbound message — from an authed API caller or a
registered channel adapter — into an agent turn whose answer is durably stored and
delivered back. This module holds only the wire/record shapes that surface crosses;
the routing-row manager, turn engine, answer store and delivery executor are the
skeleton's, reached through its ``AppConversations`` facet.
"""

from __future__ import annotations

import json
import re
import string
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

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
from tai42_contract.entry_params import (
    ENTRY_PARAM_KEY_RE,
    ENTRY_PARAM_VALUE_MAX_CHARS,
    ENTRY_PARAMS_MAX_COUNT,
    ENTRY_PARAMS_MAX_TOTAL_BYTES,
    validate_entry_params,
)
from tai42_contract.errors import ErrorKind
from tai42_contract.interactions.models import LocationElement, MediaItem, check_media_list
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, expression_annotation

#: Which door a route is reached through: ``api`` delivers by signed callback,
#: ``channel`` delivers back through the medium adapter's ``notify``.
ConversationDoor = Literal["api", "channel"]

#: What an inbound turn is routed to: ``agent`` runs a registered agent (threaded
#: conversation memory), ``tool`` dispatches a registered tool statelessly per message.
#: Defined in the leaf :mod:`tai42_contract.conversation_target` so the states models
#: reach it without importing this package (which depends on channels + interactions);
#: re-exported here so ``from tai42_contract.conversations import ConversationTargetKind``
#: is unchanged.
from tai42_contract.conversation_target import ConversationTargetKind  # noqa: E402

#: A per-target-kind bind validator a plugin registers on the conversations facet:
#: given a route's target NAME, returns the BLOCKING message lines that forbid binding
#: it to a route (empty = allow). Route creation consults every registered validator
#: for the target's kind before the route exists, so a flow that reads an unbound state
#: is refused at bind, not discovered at run time. Blocking only — a warning is not a
#: bind-path concept.
TargetBindValidator = Callable[[str], Awaitable[Sequence[str]]]

#: A thread's conversation control mode: ``agent`` runs the target turn (an agent run or a
#: tool dispatch); ``manual`` suppresses the target turn so an operator answers by hand,
#: while platform control turns (pairing, first-contact greeting) still run.
ConversationMode = Literal["agent", "manual"]

#: The mode values, for the store and the doors that validate an override.
CONVERSATION_MODES: tuple[ConversationMode, ...] = ("agent", "manual")

#: A turn's outcome kind. ``answered``/``error`` carry answer text (``error`` is generic
#: client-safe text only); ``silent`` is a deliberate no-reply and carries NO answer text.
AnswerStatus = Literal["answered", "error", "silent"]

# Route names key the ``bridge:{route_name}:{address}`` thread namespace, so the
# vocabulary must exclude ``:``.
ROUTE_NAME_RE = re.compile(r"^[a-z0-9-]+$")

# Inbound-form transport bounds. The platform carries a guest's structured submission (an
# ask-less form's answers) to the turn as opaque, untrusted data; these bounds cap the
# transport alone (JSON-object shape, string keys, finite numbers, nesting depth, total
# serialized size) — never the submission's meaning or its conformance to any schema.
INBOUND_FORM_MAX_BYTES = 32 * 1024
INBOUND_FORM_MAX_DEPTH = 64


def validate_bounded_object(value: object, *, what: str) -> dict[str, Any]:
    """Refuse (``ValueError`` naming the first violated bound) or return the dict unchanged.

    The shared bounded-transport check for an opaque caller-supplied JSON object; ``what``
    names the object in every message. Checks, in order: the value is a JSON object (a dict —
    never a list or a scalar); every object key is a string (a non-string key would be
    silently coerced by serialization, altering the object); nesting stays within
    ``INBOUND_FORM_MAX_DEPTH`` container levels — checked ITERATIVELY, so an arbitrarily deep
    (or self-referential) payload is a clean refusal, never a ``RecursionError`` from the
    interpreter stack; every number is finite (``allow_nan=False`` — ``NaN``/``Infinity`` are
    not JSON) and every value JSON-serializable; the serialized form fits in
    ``INBOUND_FORM_MAX_BYTES`` UTF-8 bytes. Messages name the violated bound and NEVER a
    submitted value — the contents are opaque data and must never surface in a log or an error.
    """
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    # Iterative depth walk: bounds nesting BEFORE the recursive ``json.dumps`` below, so a
    # deeply nested payload refuses cleanly here instead of overflowing the interpreter
    # stack. A self-referential container revisits itself one level deeper each time, so it
    # trips the same bound rather than looping.
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > INBOUND_FORM_MAX_DEPTH:
            raise ValueError(f"{what} nests deeper than the {INBOUND_FORM_MAX_DEPTH} container levels allowed")
        if isinstance(node, dict):
            children = cast("Mapping[object, object]", node)
            for key in children:
                if not isinstance(key, str):
                    raise ValueError(f"{what} object keys must be strings")
            stack.extend((child, depth + 1) for child in children.values())
        elif isinstance(node, (list, tuple)):
            stack.extend((child, depth + 1) for child in cast("Sequence[object]", node))
    try:
        serialized = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except ValueError as exc:
        raise ValueError(f"{what} numbers must be finite (NaN and Infinity are not JSON)") from exc
    except TypeError as exc:
        raise ValueError(f"{what} must contain only JSON-serializable values") from exc
    total_bytes = len(serialized.encode())
    if total_bytes > INBOUND_FORM_MAX_BYTES:
        raise ValueError(f"{what} serializes to {total_bytes} bytes, over the {INBOUND_FORM_MAX_BYTES} allowed")
    # Honest by construction: the walk above verified every object key is a string.
    return cast("dict[str, Any]", value)


def validate_inbound_form(form: object) -> dict[str, Any]:
    """Refuse (``ValueError``) or return the guest submission dict unchanged — the ask-less
    form's answers bounded as pure transport by :func:`validate_bounded_object` (``what="form"``);
    the contents stay opaque, untrusted guest data, never schema-conformant."""
    return validate_bounded_object(form, what="form")


class BlankInboundTextError(ValueError):
    """The channel door was handed a blank/whitespace-only message body — nothing to run
    a turn on. Raised by ``AppConversations.accept``; a channel adapter catches it and
    drops the inbound (log + ack), exactly as it drops an unrouted one."""


class DeliveryReceipt(StrEnum):
    """The terminal fate of an outbound message, as a channel adapter reports it back
    through ``AppConversations.record_delivery_status``. A channel normalizes its
    provider's vocabulary to these two; intermediate states are not modelled.
    """

    DELIVERED = "delivered"
    FAILED = "failed"


def _is_https_url(value: str) -> bool:
    # Must parse (not ``startswith``): rejects hostless ``https://``, scheme-only or
    # relative strings, ``user@host`` authority spoofing, and malformed authorities.
    try:
        split = urlsplit(value)
    except ValueError:
        return False
    return split.scheme == "https" and bool(split.hostname) and "@" not in split.netloc


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
            "A structured guest submission (an ask-less form's answers) riding WITH the "
            "text. ``text`` stays required and non-blank — it is the CARRIER every reader "
            "consumes: a channel submits a faithful text form of the submission alongside "
            "the structured data, so a form-unaware consumer still sees the whole turn, "
            "and the ``attachments``/``location`` siblings ride the same pattern. The "
            "platform attaches no meaning and NO TRUST to the contents: guest-shaped "
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
        # a guest message. Intentionally named ``schema`` (matches the payload it carries);
        # shadows the deprecated ``BaseModel.schema()`` alias, which this model never uses.
        schema: dict[str, Any] | None = None  # pyright: ignore[reportIncompatibleMethodOverride]

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


class ConversationRouteCreate(BaseModel):
    """The client-facing create/edit body for a conversation route: the fields a caller
    supplies.

    Binds a ``(target_kind, target_name)`` — an ``agent`` run or a ``tool`` dispatch — to
    an ``execution_key`` the turn runs AS (bound with pass-role at create). A ``tool``
    target may carry a ``payload_expr`` (jq mapping the inbound message to the tool's
    kwargs) and a ``reply_expr`` (jq mapping the tool's result to the reply); both are
    tool-only. ``api`` rows carry an https ``callback_url``; ``channel`` rows carry the
    registry ``channel`` plus the ``our_identity`` the medium is texted at (N rows may
    share a channel, each its own identity). The server-derived ``callback_secret`` and
    ``execution_key_fingerprint`` are deliberately absent; :class:`ConversationRoute` is
    this shape plus those. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    # A ``:``-free slug: it keys the ``bridge:{route_name}:{client_address}`` thread
    # namespace, where a ``:`` would let one route's threads collide with another's.
    route_name: str
    door: ConversationDoor
    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    # tool targets only: a jq program mapping the inbound payload to the tool kwargs, and
    # one mapping the tool result to the reply. Compiled at create; an ``agent`` target
    # carries neither. ``reply_expr`` maps the SUCCESS shape: a result whose own ``status``
    # names a non-success terminal diverts to the turn's error outcome without being mapped.
    # Both carry the ``x-tai42-expression`` schema annotation (via ``Annotated`` so the
    # attribute default stays the ``None`` literal — the api-gate flags a ``Field(default=...)``
    # redeclaration as breaking) so a schema-driven UI auto-renders the jq editor.
    payload_expr: Annotated[
        str | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="payload expression",
                    blurb="the inbound turn payload the route maps to the tool/flow kwargs",
                    keys=[
                        ("message", "the inbound message text"),
                        ("sender", "the sending address"),
                        ("our_identity", "door=channel: the medium address we are texted at"),
                        ("channel", "door=channel: the registry channel name"),
                        ("thread_id", "the turn's canonical thread id (the thread doors' id)"),
                        ("person_id", "multichannel only: the linked person's id"),
                        ("person_addresses", "multichannel only: the person's known addresses"),
                        ("params", "non-empty entry params, nested under this key"),
                        ("form", "a structured form submission, present only when the inbound carried one"),
                        ("attachments", "inbound media the guest sent, present only when the inbound carried some"),
                        ("location", "a geographic point the guest shared, present only when the inbound carried one"),
                        ("turn", "the turn ids: {id, inbound: {id, kind, source}}"),
                        ("event", "an event turn's structured payload {id, kind, payload}; absent on a message turn"),
                    ],
                    returns="the JSON object dispatched as the tool/flow kwargs",
                )
            }
        ),
    ] = None
    reply_expr: Annotated[
        str | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="reply expression",
                    blurb="the tool/flow result (the SUCCESS shape) the route maps to the guest reply",
                    returns="the reply: null (silent), a string, or a list of answer parts",
                )
            }
        ),
    ] = None
    # The thread's control mode when no per-thread override is set: ``agent`` runs the
    # target turn, ``manual`` suppresses it for an operator to answer.
    initial_mode: ConversationMode = "agent"
    execution_key: str = Field(
        min_length=1,
        description=(
            "The api-key ``user_id`` the turn runs AS; its live stored grants authorize "
            "the agent run or tool dispatch and every tool call the turn makes."
        ),
    )
    channel: str | None = None  # door=channel: the registry name, ``:``-free
    our_identity: str | None = None  # door=channel: the medium address we are texted at
    callback_url: str | None = None  # door=api: the https answer sink
    # A per-route override of the global ``per_address_turns_per_hour`` cap: the positive
    # per-hour turn rate this route's per-address buckets run at, or ``None`` to run at the
    # global rate.
    turns_per_hour_override: int | None = Field(default=None, gt=0)
    # The guest-facing reply sent when a conversational turn on this route fails; ``None`` uses
    # the built-in English default. LITERAL text — no placeholders/templating. Non-blank when
    # set. (No other text field in this file carries a max_length; 2000 is a defensible bound
    # for a single guest-facing reply.)
    error_reply_text: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("route_name")
    @classmethod
    def _check_route_name(cls, value: str) -> str:
        if not ROUTE_NAME_RE.fullmatch(value):
            raise ValueError(f"route_name must be a slug matching {ROUTE_NAME_RE.pattern!r}: {value!r}")
        return value

    @field_validator("error_reply_text")
    @classmethod
    def _check_error_reply_text(cls, value: str | None) -> str | None:
        # A set override must be non-blank: a whitespace-only reply would deliver an empty
        # guest-facing message where the built-in default was intended.
        if value is not None and not value.strip():
            raise ValueError("error_reply_text must be non-blank when set")
        return value

    @model_validator(mode="after")
    def _check_target_fields(self) -> ConversationRouteCreate:
        if self.target_kind == "agent" and (self.payload_expr is not None or self.reply_expr is not None):
            raise ValueError("target_kind=agent carries no payload_expr/reply_expr")
        return self

    @model_validator(mode="after")
    def _check_door_fields(self) -> ConversationRouteCreate:
        if self.door == "channel":
            if not (self.channel and self.channel.strip()):
                raise ValueError("door=channel requires a non-blank channel")
            if ":" in self.channel:
                # The channel name prefixes the inbound-dedupe and outbound-index keys, so
                # a ``:`` in it shifts the separator and lets one channel read another's.
                raise ValueError(f"door=channel requires a channel name free of ':': {self.channel!r}")
            if not (self.our_identity and self.our_identity.strip()):
                raise ValueError("door=channel requires a non-blank our_identity")
            if self.callback_url is not None:
                raise ValueError("door=channel carries no callback_url")
        else:
            if not (self.callback_url and _is_https_url(self.callback_url)):
                raise ValueError("door=api requires an absolute https callback_url")
            if self.channel is not None:
                raise ValueError("door=api carries no channel")
            if self.our_identity is not None:
                raise ValueError("door=api carries no our_identity")
        return self


class ConversationRoute(ConversationRouteCreate):
    """The stored routing row: :class:`ConversationRouteCreate` plus the two
    server-derived fields. What the manager persists and backup restore validates.

    ``callback_secret`` (``api`` rows) signs the delivery callback; it is excluded from
    export and re-minted per row on import, so callbacks signed with the pre-import
    secret no longer verify. ``execution_key_fingerprint`` is the bound key's per-mint
    identity: a turn matches it against the live key, so a revoke+remint of the same
    ``user_id`` fails closed.
    """

    callback_secret: str | None = None
    execution_key_fingerprint: str = Field(
        min_length=1,
        description=(
            "The bound key's per-mint identity the turn resolves against, derived "
            "server-side at create and never client-supplied."
        ),
    )


class PersonAddress(BaseModel):
    """One reachable endpoint of a :class:`Person` on a single target — one channel address
    (or api caller address) the platform folds into that person's identity.

    A ``channel``-door address carries the registry ``channel`` name plus the
    ``our_identity`` the medium is texted at; an ``api``-door address carries ``None`` for
    both — the api caller address is the composed ``caller/end-user`` string, which has no
    channel identity. ``routes`` is EVERY route name this address has written under on the
    target (ordered, deduped): one address can legally reach one target through several
    routes (N api routes; a channel identity re-routed under a new route name), and the
    aggregated person-thread read enumerates the person's routes straight off these rows, so
    a scalar here would silently drop legs. ``address`` is the canonical form the bridge
    already keys threads by. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    door: ConversationDoor
    routes: list[str] = Field(min_length=1)
    channel: str | None = None
    our_identity: str | None = None
    address: str = Field(min_length=1)
    linked_at: datetime

    @field_validator("routes")
    @classmethod
    def _routes_non_blank_and_unique(cls, value: list[str]) -> list[str]:
        if any(not route.strip() for route in value):
            raise ValueError("every route name must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError(f"route names must be unique, got {value!r}")
        return value

    @model_validator(mode="after")
    def _channel_identity_matches_door(self) -> PersonAddress:
        if self.door == "channel":
            if not (self.channel and self.channel.strip()):
                raise ValueError("a channel-door address requires a non-blank channel")
            if not (self.our_identity and self.our_identity.strip()):
                raise ValueError("a channel-door address requires a non-blank our_identity")
        elif self.channel is not None or self.our_identity is not None:
            raise ValueError("an api-door address carries no channel/our_identity")
        return self

    @field_validator("linked_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # The stored form is compared lexically to pick a merge survivor, so it must
        # carry a UTC offset (a naive value sorts before every ``+00:00`` and corrupts
        # the survivor rule); reject a naive datetime and normalize any aware value to
        # UTC (same strictness as InteractionRequest/ChannelDelivery).
        if value.tzinfo is None:
            raise ValueError("linked_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @field_serializer("linked_at")
    def _serialize_linked_at(self, value: datetime) -> str:
        # A single canonical ISO-8601 form for the stored timestamp — see
        # :meth:`Person._serialize_created_at` for why the format is pinned.
        return value.isoformat()


class Person(BaseModel):
    """A single identity on one target: the one-or-more :class:`PersonAddress` rows the
    platform treats as the same person for a ``(target_kind, target_name)`` pair.

    A provisional person carries exactly ONE address — its first contact. Explicit pair-code
    redemption merges two persons into one (the union of their addresses); persons never
    cross targets. There is NO greeted flag: the row's existence is itself the first-contact
    marker, so a caller learns a first contact from whether it created the row. Runtime state
    that lives only in the deployment's Redis — deliberately NOT a backup section, the same
    family as a conversation record. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    person_id: str = Field(min_length=1)
    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    created_at: datetime
    addresses: list[PersonAddress] = Field(min_length=1)

    @field_validator("created_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # The stored form is compared lexically to pick a merge survivor, so it must
        # carry a UTC offset (a naive value sorts before every ``+00:00`` and corrupts
        # the survivor rule); reject a naive datetime and normalize any aware value to
        # UTC (same strictness as InteractionRequest/ChannelDelivery).
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        # The stored form is compared lexically to pick a merge survivor (earliest first),
        # so every timestamp must serialize to ONE canonical, chronologically-sortable
        # string. ``isoformat`` on a tz-aware UTC value is that form; the default pydantic
        # serializer's trailing ``Z`` would not compare equal to a hand-built ``+00:00``.
        return value.isoformat()


class PairCodeInvalidError(Exception):
    """A submitted pair code did not resolve to a live single-use record. Deliberately
    UNIFORM across unknown / expired / already-redeemed: the three are indistinguishable to
    the caller (no oracle), so a redeem reply never reveals whether a code ever existed."""

    # The submitted code did not work; deliberately uniform across unknown/expired/redeemed (no oracle).
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class NotLinkedError(Exception):
    """An unlink was asked of an address that is not part of a multi-address person — it is
    already its own provisional person, so there is nothing to detach."""

    # A state-dependent refusal: the address is not part of a multi-address person, so there is nothing to detach.
    __tai_error_kind__ = ErrorKind.CONFLICT


class MultichannelDisabledError(Exception):
    """A pairing operation was attempted against a target whose multichannel support is off.
    The pairing tool refuses with this; the ``/link`` and ``/unlink`` commands instead pass
    through as ordinary text on such a target."""

    # The target's multichannel capability is off — a capability refusal, mirroring NotSupported -> UNAVAILABLE.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class CrossTargetMergeError(Exception):
    """A merge was attempted across two different targets. Persons are per-target and can
    never span targets; a NAMED type so a pairing turn scopes it distinctly from an
    infrastructure fault."""

    # Structurally impossible by construction (persons never span targets) — an
    # invalid request, not a current-state conflict.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


# The one placeholder a greeting template may reference — the mint-at-greeting-time pair
# code. Any other ``{...}`` field is refused at write time so a typo cannot render literally.
GREETING_PLACEHOLDER = "pairing_code"


def _check_greeting_placeholders(template: str) -> None:
    """Refuse a greeting template that references anything but ``{pairing_code}``.

    Parsed exactly as :meth:`str.format` would render it, so a malformed template (an
    unbalanced brace), an auto-numbered ``{}``, a foreign field name, or a
    ``{pairing_code}`` carrying a conversion/format-spec/attribute access is refused here —
    at the write — rather than rendering wrong or raising when the greeting fires."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"greeting_template is not a valid template: {exc}") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            # Literal text, including the escaped ``{{``/``}}`` braces.
            continue
        if field_name != GREETING_PLACEHOLDER or conversion is not None or format_spec:
            raise ValueError(
                f"greeting_template may reference only the {{{GREETING_PLACEHOLDER}}} placeholder, "
                f"got an unsupported field {field_name!r}"
            )


class TargetConversationConfig(BaseModel):
    """Per-target configuration for the conversation bridge, keyed by
    ``(target_kind, target_name)`` — the agent or tool an inbound turn is routed to.

    ``multichannel`` opts the target into person linking; ``greeting_template`` is the
    first-contact greeting, which may reference at most the ``{pairing_code}`` placeholder
    (minted at greeting time). The row carries no server-derived fields, so it IS its own
    create payload — no Create/stored split. An unknown ``{...}`` placeholder is refused so
    a typo cannot render literally; a blank template string is refused, because ``None`` is
    the explicit spelling for "no greeting". Frozen.
    """

    model_config = ConfigDict(frozen=True)

    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    multichannel: bool = False
    greeting_template: str | None = None

    @field_validator("target_name")
    @classmethod
    def _non_blank_target_name(cls, value: str) -> str:
        # It keys the config row; a blank segment would collide distinct targets onto one key.
        if not value.strip():
            raise ValueError("target_name must be non-blank")
        return value

    @field_validator("greeting_template")
    @classmethod
    def _check_greeting_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("greeting_template must be non-blank (use null for no greeting)")
        _check_greeting_placeholders(value)
        return value


__all__ = [
    "CONVERSATION_MODES",
    "ENTRY_PARAMS_MAX_COUNT",
    "ENTRY_PARAMS_MAX_TOTAL_BYTES",
    "ENTRY_PARAM_KEY_RE",
    "ENTRY_PARAM_VALUE_MAX_CHARS",
    "EVENT_ID_MAX_CHARS",
    "EVENT_KIND_RE",
    "GREETING_PLACEHOLDER",
    "INBOUND_FORM_MAX_BYTES",
    "INBOUND_FORM_MAX_DEPTH",
    "ROUTE_NAME_RE",
    "AnswerPart",
    "AnswerStatus",
    "BlankInboundTextError",
    "ConversationAnswer",
    "ConversationDoor",
    "ConversationEvent",
    "ConversationEventSubmission",
    "ConversationMessage",
    "ConversationMode",
    "ConversationRoute",
    "ConversationRouteCreate",
    "ConversationTargetKind",
    "CrossTargetMergeError",
    "DeliveryReceipt",
    "MultichannelDisabledError",
    "NotLinkedError",
    "PairCodeInvalidError",
    "Person",
    "PersonAddress",
    "TargetBindValidator",
    "TargetConversationConfig",
    "joined_answer_text",
    "validate_bounded_object",
    "validate_entry_params",
    "validate_inbound_form",
]
