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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from tai42_contract.channels import (
    NOTIFICATION_MESSAGE_MAX_CHARS,
    NOTIFICATION_OPTION_MAX_CHARS,
    NOTIFICATION_OPTIONS_MAX,
    ChannelTemplate,
)
from tai42_contract.errors import ErrorKind
from tai42_contract.interactions.models import MediaItem, check_media_list

#: Which door a route is reached through: ``api`` delivers by signed callback,
#: ``channel`` delivers back through the medium adapter's ``notify``.
ConversationDoor = Literal["api", "channel"]

#: What an inbound turn is routed to: ``agent`` runs a registered agent (threaded
#: conversation memory), ``tool`` dispatches a registered tool statelessly per message.
ConversationTargetKind = Literal["agent", "tool"]

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

# Entry-param transport bounds. The platform carries opaque caller-supplied params to a
# tool target's payload and attaches NO meaning and NO trust; these bounds cap the
# transport alone (count, key shape, value length, total serialized size).
ENTRY_PARAMS_MAX_COUNT = 16
ENTRY_PARAM_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENTRY_PARAM_VALUE_MAX_CHARS = 512
ENTRY_PARAMS_MAX_TOTAL_BYTES = 2048  # len(json.dumps(params, separators=(",", ":"), sort_keys=True).encode())


def validate_entry_params(params: dict[str, str]) -> dict[str, str]:
    """Refuse (``ValueError`` naming the first violated bound) or return the dict unchanged.

    Checks, in order: count, key regex, value type is ``str``, value length, total
    serialized bytes. The message names the violated bound and MAY name the offending KEY,
    but NEVER a value — an entry-param value is opaque and must never surface in a log or an
    error. Duplicate keys cannot reach a dict; the web door refuses duplicates at parse time
    before building the dict.
    """
    if len(params) > ENTRY_PARAMS_MAX_COUNT:
        raise ValueError(f"params carries {len(params)} keys, over the {ENTRY_PARAMS_MAX_COUNT} allowed")
    # Values are validated as untrusted objects: the annotation promises ``str``, but a
    # non-str value would serialize silently through ``json.dumps`` below, so the type is
    # enforced loudly here (the object view keeps the check genuine, not redundant).
    untrusted = cast("Mapping[str, object]", params)
    for key, value in untrusted.items():
        if not ENTRY_PARAM_KEY_RE.fullmatch(key):
            raise ValueError(f"params key {key!r} must match {ENTRY_PARAM_KEY_RE.pattern!r}")
        if not isinstance(value, str):
            raise ValueError(f"params value for key {key!r} must be a string")
        if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
            raise ValueError(f"params value for key {key!r} is over the {ENTRY_PARAM_VALUE_MAX_CHARS}-character limit")
    total_bytes = len(json.dumps(params, separators=(",", ":"), sort_keys=True).encode())
    if total_bytes > ENTRY_PARAMS_MAX_TOTAL_BYTES:
        raise ValueError(f"params serialize to {total_bytes} bytes, over the {ENTRY_PARAMS_MAX_TOTAL_BYTES} allowed")
    return params


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


class AnswerPart(BaseModel):
    """One message of an ordered multi-message answer — the platform's rich part shape.

    It mirrors :class:`ChannelNotification`'s CONTENT surface exactly (``message`` plus the
    optional ``media`` / ``template`` / ``options`` richer-send forms and their validators),
    MINUS the per-delivery routing fields (``recipient`` / ``sender_identity``) — those stay
    on the single delivery, never per part. The delivery machine sends each part as its own
    ``ChannelNotification``, in order, chunking the ``message`` text at the channel width and
    carrying the part's media/template/options alongside it, exactly as a single notification
    does today.

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

    message: str = ""  # human-readable text; blank/omitted ONLY for a media-only part (media carries it)
    media: list[MediaItem] | None = None  # display media sent WITH the message; None -> none
    template: ChannelTemplate | None = None  # out-of-window template send; None -> freeform
    options: list[str] | None = None  # tappable options; a tap enters the conversation; None -> none

    @field_validator("message")
    @classmethod
    def _message_valid(cls, value: str) -> str:
        # Mirrors ChannelNotification.message: length cap only here, with the blank-vs-media
        # rule decided in :meth:`_message_or_media` once every field is bound.
        if len(value) > NOTIFICATION_MESSAGE_MAX_CHARS:
            raise ValueError(f"message must be at most {NOTIFICATION_MESSAGE_MAX_CHARS} characters, got {len(value)}")
        return value

    @field_validator("media")
    @classmethod
    def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        if value is not None:
            check_media_list(value)
        return value

    @field_validator("options")
    @classmethod
    def _options_valid(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("options must be a non-empty list when present")
        if len(value) > NOTIFICATION_OPTIONS_MAX:
            raise ValueError(f"options carries at most {NOTIFICATION_OPTIONS_MAX} entries, got {len(value)}")
        for option in value:
            if not option.strip():
                raise ValueError("each option must be a non-blank string")
            if len(option) > NOTIFICATION_OPTION_MAX_CHARS:
                raise ValueError(
                    f"each option must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(option)}"
                )
        return value

    @model_validator(mode="after")
    def _message_or_media(self) -> AnswerPart:
        # Mirrors ChannelNotification._message_or_media: a part is non-blank text OR a
        # media-only part (blank message carried by non-empty media). A blank message with no
        # media has nothing to deliver; options require a non-blank message (a choice needs a
        # prompt); a template rides a non-blank message (its ``not self.media`` branch).
        if not self.message.strip():
            if not self.media:
                raise ValueError("message must be non-blank unless media carries the content")
            if self.options is not None:
                raise ValueError("a media-only (blank-message) part carries no options; a choice needs a prompt")
        return self

    @model_validator(mode="after")
    def _media_template_exclusive(self) -> AnswerPart:
        if self.media is not None and self.template is not None:
            raise ValueError("media and template are mutually exclusive on one part")
        return self

    @model_validator(mode="after")
    def _options_template_exclusive(self) -> AnswerPart:
        if self.options is not None and self.template is not None:
            raise ValueError("options and template are mutually exclusive on one part")
        return self

    def is_plain_text(self) -> bool:
        """Whether this part carries nothing beyond its ``message`` — the case a
        single-message answer degenerates to (``parts`` then adds nothing over the joined
        ``answer`` and is dropped). A media-only part (blank message carrying media) is NOT
        plain text — it adds the media the joined ``answer`` cannot carry."""
        return self.media is None and self.template is None and self.options is None


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
    # carries neither.
    payload_expr: str | None = None
    reply_expr: str | None = None
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
    "GREETING_PLACEHOLDER",
    "ROUTE_NAME_RE",
    "AnswerPart",
    "AnswerStatus",
    "BlankInboundTextError",
    "ConversationAnswer",
    "ConversationDoor",
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
    "TargetConversationConfig",
    "joined_answer_text",
    "validate_entry_params",
]
