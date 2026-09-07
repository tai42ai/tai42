"""Channel delivery contracts.

A :class:`Channel` pushes an ``ask_user`` question to a human on a specific
medium (Telegram, Slack, SMS, ...) and bridges the human's reply back into the
interactions store by forwarding it to the delivery's public ``callback_url``.
Channels are registered on the app handle (``tai42_app.channels``) by channel
plugins and looked up by name when ``ask_user`` is called with ``channel=...``.
Delivery either returns ``None`` (success) or raises
:class:`ChannelDeliveryError` (any failure) — never a bool.

A channel also sends fire-and-forget notifications: ``notify`` pushes one
:class:`ChannelNotification` to a human with no interaction, no ticket, and no
reply path, under the same loud-failure rule; it returns the per-message ids the
medium assigned the send (empty when the medium exposes none), never a bool.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.entry_params import validate_entry_params
from tai42_contract.errors import ErrorKind
from tai42_contract.interactions.models import (
    MEDIA_CAPTION_MAX_CHARS,
    MISMATCH_NOTICE_MAX_CHARS,
    AnswerFormat,
    AnswerMismatchPolicy,
    FormData,
    FormPage,
    LocationElement,
    MediaItem,
    MediaKind,
    check_media_list,
    validate_action_url,
)


class ChannelDeliveryError(Exception):
    """Raised by a :class:`Channel` when delivering a question fails.

    Every failure mode — an unreachable medium API, a rejected send, a missing
    credential, a misconfigured recipient — raises this single typed error.
    ``deliver`` NEVER returns a bool and NEVER silently drops a message: an
    undeliverable question is a loud failure, so the only success signal is a
    plain return.

    ``retryable`` classifies the failure for the caller's retry decision: True
    means transient (a medium 5xx, a rate limit, a transport fault or timeout)
    and a fresh attempt may land. It defaults to False — a rejected recipient, a
    bad credential, and any unrecognised fault fail on the first try rather than
    being blind-retried. ``retry_after`` is the seconds the medium asked the
    caller to wait (an HTTP ``Retry-After``, say); meaningful only when
    ``retryable`` is True.
    """

    # A question handed to a channel could not be delivered to the human.
    __tai_error_kind__ = ErrorKind.DELIVERY_FAILED

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ChannelInputError(Exception):
    """A permanent refusal of the input's shape or content by the channel.

    The input is contract-valid but the medium cannot render it BY NATURE — e.g. a
    ``data:`` image URL to a channel that sends only public ``https`` sources.
    Retrying the same input can never succeed, so this is distinct from
    :class:`ChannelDeliveryError` (a delivery failure the caller may retry): the
    operation door maps it to the client-error (400) class, never the retryable
    503 a transient delivery failure earns.
    """

    # The input's shape/content is contract-valid but unrenderable by nature — a
    # permanent client-side refusal.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


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
            # non-blank + guest-reply cap the ask REQUEST (``InteractionRequest``) enforces,
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


# Caps on a template's component parameters. ``TEMPLATE_BUTTONS_MAX`` bounds the buttons
# component; ``TEMPLATE_PARAM_MAX_CHARS`` bounds one substituted value (a body text param, a
# button payload, a URL suffix) — a generous single-value bound, never a message body.
TEMPLATE_BUTTONS_MAX = 10
TEMPLATE_PARAM_MAX_CHARS = 4096


class QuickReplyButtonParam(BaseModel):
    """The runtime argument for one QUICK-REPLY button of a template's buttons component:
    ``payload`` is the string the medium returns when the human taps the button. Frozen."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["quick_reply"] = "quick_reply"
    payload: str

    @field_validator("payload")
    @classmethod
    def _payload_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quick-reply button payload must be non-blank")
        if len(value) > TEMPLATE_PARAM_MAX_CHARS:
            raise ValueError(
                f"quick-reply button payload must be at most {TEMPLATE_PARAM_MAX_CHARS} characters, got {len(value)}"
            )
        return value


class UrlButtonParam(BaseModel):
    """The runtime argument for one URL button of a template's buttons component:
    ``url_parameter`` is the dynamic suffix substituted into the button's pre-approved URL.
    Frozen."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["url"] = "url"
    url_parameter: str

    @field_validator("url_parameter")
    @classmethod
    def _url_parameter_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url button parameter must be non-blank")
        if len(value) > TEMPLATE_PARAM_MAX_CHARS:
            raise ValueError(
                f"url button parameter must be at most {TEMPLATE_PARAM_MAX_CHARS} characters, got {len(value)}"
            )
        return value


# One button's runtime argument on a template's buttons component: a quick-reply payload or a URL
# suffix, discriminated on ``kind``. Positional — the i-th entry parameterises the i-th button.
TemplateButtonParam = Annotated[QuickReplyButtonParam | UrlButtonParam, Field(discriminator="kind")]


def _no_button_params() -> list[TemplateButtonParam]:
    # A concretely-typed default factory for the buttons component (an empty list default over the
    # discriminated ``TemplateButtonParam`` alias), so the field's element type stays fully known.
    return []


class ChannelTemplate(BaseModel):
    """A pre-approved, named template a channel sends outside its freeform window.

    Some media only accept arbitrary text inside a bounded conversation window (e.g. WhatsApp's
    24-hour customer-service window); outside it, the sole accepted send is an operator-authored
    template referenced by ``name`` in an approved ``language`` — both required and non-blank.

    The template's runtime arguments are its NAMED components (never one flat positional list):

    * ``header_media`` — the media argument for a media HEADER component (a display item:
      image/document/video/audio, never a ``link``); ``None`` when the template has no media
      header (or a static/text header needing no argument).
    * ``body_parameters`` — the POSITIONAL body-text values substituted into the body's
      placeholders in order; empty when the body has no placeholders. Typed values (currency,
      date-time) ride as their pre-formatted STRING here — the contract does not model the type.
    * ``buttons`` — the POSITIONAL per-button arguments of the buttons component
      (:data:`TemplateButtonParam`: a :class:`QuickReplyButtonParam` payload or a
      :class:`UrlButtonParam` url suffix); the i-th entry parameterises the i-th button, at most
      ``TEMPLATE_BUTTONS_MAX``. Empty when no button needs a runtime argument.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    language: str
    header_media: MediaItem | None = None
    body_parameters: list[str] = Field(default_factory=list)
    buttons: list[TemplateButtonParam] = Field(default_factory=_no_button_params)

    @field_validator("name", "language")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-blank")
        return value

    @field_validator("header_media")
    @classmethod
    def _header_media_valid(cls, value: MediaItem | None) -> MediaItem | None:
        if value is not None and value.kind is MediaKind.LINK:
            raise ValueError("template header_media must be a display item (image/document/video/audio), not a link")
        return value

    @field_validator("body_parameters")
    @classmethod
    def _body_parameters_valid(cls, value: list[str]) -> list[str]:
        for param in value:
            if not param.strip():
                raise ValueError("each body parameter must be non-blank")
            if len(param) > TEMPLATE_PARAM_MAX_CHARS:
                raise ValueError(
                    f"each body parameter must be at most {TEMPLATE_PARAM_MAX_CHARS} characters, got {len(param)}"
                )
        return value

    @field_validator("buttons")
    @classmethod
    def _buttons_capped(cls, value: list[TemplateButtonParam]) -> list[TemplateButtonParam]:
        if len(value) > TEMPLATE_BUTTONS_MAX:
            raise ValueError(f"buttons carries at most {TEMPLATE_BUTTONS_MAX} entries, got {len(value)}")
        return value


# Upper bound on a notification's tappable option list — one notification cannot fan
# out an unbounded set of tappable options; matches the richest interactive-list medium.
NOTIFICATION_OPTIONS_MAX = 10

# Generous abuse bound on a notification's message text — not a UX limit; channels may
# impose tighter limits. Caps what persists into replayed transcript streams.
NOTIFICATION_MESSAGE_MAX_CHARS = 65536

# Per-option character bound — same cap as the media caption, the other short label that
# rides a replayed frame.
NOTIFICATION_OPTION_MAX_CHARS = MEDIA_CAPTION_MAX_CHARS

# Abuse bound on a notification address (``recipient``, ``sender_identity``) and the sink's
# ``audience`` identity — short routing values, not message bodies. Each persists into the
# replayed feed record and ``audience`` also becomes a per-identity Redis key, so an
# unbounded value would bloat a stored frame or mint an oversized key.
NOTIFICATION_ADDRESS_MAX_CHARS = 512

# Upper bound on a sectioned option list's section count. A sectioned list groups its rows
# under titled sections; the summed row count across every section still obeys
# ``NOTIFICATION_OPTIONS_MAX`` (one message cannot fan out an unbounded tap set).
NOTIFICATION_SECTIONS_MAX = 10

# Per-footer character bound — the short trailing line under an interactive message; same cap
# as an option label, the other short rider on an interactive frame.
NOTIFICATION_FOOTER_MAX_CHARS = MEDIA_CAPTION_MAX_CHARS

# Bound on an AUTHORED reply-option id — the stable id a sender may set on a quick-reply button
# or a list row so the tap echoes it back verbatim. Set to the STRICTEST carrier's cap (WhatsApp
# limits an interactive button/row id to 256 characters); a channel that mints its own id when
# the author sets none is unaffected.
OPTION_ID_MAX_CHARS = 256


class ReplyOption(BaseModel):
    """A tappable suggested reply. Tapping SUBMITS ``text`` as the guest's next inbound message —
    the quick-reply / list-row case, where the option's own text becomes the turn. ``description``
    is an OPTIONAL secondary line a sectioned-list row renders under its ``text``; a channel that
    renders flat buttons (no descriptions) ignores it.

    ``id`` is an OPTIONAL author-set stable identifier for the button/list row. When set, a channel
    sends it verbatim on the wire and the guest's tap echoes it back (a channel surfaces the echoed
    id to the inbound turn as opaque enrichment — e.g. Slack forwards it as ``params.reply_id``);
    when ``None`` the channel mints its own id as today. Bounded by ``OPTION_ID_MAX_CHARS`` and a
    single-line non-blank label — the strictest carrier's rule. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["reply"] = "reply"
    text: str
    description: str | None = None
    id: str | None = None

    @field_validator("text")
    @classmethod
    def _text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply option text must be non-blank")
        if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
            raise ValueError(
                f"reply option text must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("reply option description must be non-blank when present")
            if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
                raise ValueError(
                    f"reply option description must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, "
                    f"got {len(value)}"
                )
        return value

    @field_validator("id")
    @classmethod
    def _id_valid(cls, value: str | None) -> str | None:
        # An author-set id is a single-line non-blank token within the strictest carrier's cap;
        # raw whitespace and control/format characters are rejected (an id rides the wire and is
        # echoed back, so it must not carry a newline or a bidi spoof).
        if value is not None:
            if not value.strip():
                raise ValueError("reply option id must be non-blank when present")
            if len(value) > OPTION_ID_MAX_CHARS:
                raise ValueError(f"reply option id must be at most {OPTION_ID_MAX_CHARS} characters, got {len(value)}")
            if any(ch.isspace() or not ch.isprintable() for ch in value):
                raise ValueError("reply option id must be a single-line token with no whitespace or control characters")
        return value


class LinkOption(BaseModel):
    """A tappable link action. Tapping OPENS ``url`` (an absolute ``http(s)`` URL) in the human's
    browser — NO message is submitted, distinct from a :class:`ReplyOption`. ``label`` is the
    button text. The URL-button / call-to-action case. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["link"] = "link"
    label: str
    url: str

    @field_validator("label")
    @classmethod
    def _label_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("link option label must be non-blank")
        if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
            raise ValueError(
                f"link option label must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("url")
    @classmethod
    def _url_valid(cls, value: str) -> str:
        return validate_action_url(value)


# One tappable option on an interactive message: EITHER a reply (tap submits its text) or a link
# action (tap opens its url). A discriminated union on ``kind`` — the input carries the tag, so a
# bare string is not an option (the clean break from the old text-only ``list[str]``: an option
# is authored as ``{"kind": "reply", "text": …}`` or ``{"kind": "link", "label": …, "url": …}``).
Option = Annotated[ReplyOption | LinkOption, Field(discriminator="kind")]


class OptionSection(BaseModel):
    """One titled section of a sectioned option list. ``title`` is the section header; ``rows`` are
    its entries — a sectioned list holds :class:`ReplyOption` rows ONLY (a tapped row submits its
    text; a link action is a button, never a list row). A present ``rows`` is non-empty. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    rows: list[ReplyOption]

    @field_validator("title")
    @classmethod
    def _title_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("section title must be non-blank")
        if len(value) > NOTIFICATION_OPTION_MAX_CHARS:
            raise ValueError(
                f"section title must be at most {NOTIFICATION_OPTION_MAX_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("rows")
    @classmethod
    def _rows_non_empty(cls, value: list[ReplyOption]) -> list[ReplyOption]:
        if not value:
            raise ValueError("section rows must be a non-empty list")
        return value


def check_options(value: list[Option] | None) -> list[Option] | None:
    """List-level caps on a flat interactive option list: None means none; a present list is
    non-empty and holds at most ``NOTIFICATION_OPTIONS_MAX`` entries. Each option's own shape
    (reply text / link label+url bounds) is the :class:`ReplyOption`/:class:`LinkOption` concern.
    Raises ``ValueError``."""
    if value is None:
        return None
    if not value:
        raise ValueError("options must be a non-empty list when present")
    if len(value) > NOTIFICATION_OPTIONS_MAX:
        raise ValueError(f"options carries at most {NOTIFICATION_OPTIONS_MAX} entries, got {len(value)}")
    return value


def check_sections(value: list[OptionSection] | None) -> list[OptionSection] | None:
    """List-level caps on a sectioned option list: None means none; a present list is non-empty,
    holds at most ``NOTIFICATION_SECTIONS_MAX`` sections, and its rows summed across every section
    stay within ``NOTIFICATION_OPTIONS_MAX`` (one message never fans out an unbounded tap set).
    Raises ``ValueError``."""
    if value is None:
        return None
    if not value:
        raise ValueError("sections must be a non-empty list when present")
    if len(value) > NOTIFICATION_SECTIONS_MAX:
        raise ValueError(f"sections carries at most {NOTIFICATION_SECTIONS_MAX} sections, got {len(value)}")
    total_rows = sum(len(section.rows) for section in value)
    if total_rows > NOTIFICATION_OPTIONS_MAX:
        raise ValueError(
            f"sections carry at most {NOTIFICATION_OPTIONS_MAX} rows in total across all sections, got {total_rows}"
        )
    return value


def check_footer(value: str | None) -> str | None:
    """A footer is the short trailing line under an interactive message: None means none; a
    present value is non-blank and within ``NOTIFICATION_FOOTER_MAX_CHARS``. Raises ``ValueError``."""
    if value is None:
        return None
    if not value.strip():
        raise ValueError("footer must be non-blank when present")
    if len(value) > NOTIFICATION_FOOTER_MAX_CHARS:
        raise ValueError(f"footer must be at most {NOTIFICATION_FOOTER_MAX_CHARS} characters, got {len(value)}")
    return value


def check_header(value: MediaItem | None) -> MediaItem | None:
    """A header is a SINGLE display-media item shown above an interactive message: None means
    none; a present item is display media (image/document/video/audio), never a ``link`` (an
    anchor is content, not a header). The item's own url/kind shape is :class:`MediaItem`'s
    concern. Raises ``ValueError``."""
    if value is not None and value.kind is MediaKind.LINK:
        raise ValueError("header media must be a display item (image/document/video/audio), not a link")
    return value


def check_interactive_composition(
    *,
    message: str,
    media: list[MediaItem] | None,
    location: LocationElement | None,
    template: ChannelTemplate | None,
    options: list[Option] | None,
    sections: list[OptionSection] | None,
    schema: dict[str, Any] | None,
    header: MediaItem | None,
    footer: str | None,
    noun: str,
) -> None:
    """The shared cross-field rules every option-carrying message carrier
    (:class:`ChannelNotification`, :class:`~tai42_contract.conversations.AnswerPart`) enforces, so
    the two can never drift. ``noun`` names the carrier for the raised messages
    (``"notification"`` / ``"part"``). Rules:

    * message is non-blank BY DEFAULT, EXCEPT blank for a CONTENT-ONLY send — a blank message
      carried by non-empty ``media`` OR a ``location`` (a caption-less image / a bare pin).
    * an interactive surface (``options``, ``sections``, ``schema``) REQUIRES a non-blank message
      — a choice or a form needs a prompt — so a content-only send carries none of them.
    * ``options`` XOR ``sections`` — one choice surface (flat buttons OR a sectioned list).
    * ``schema`` is exclusive with both ``options`` and ``sections`` — one interactive surface
      (a form's fields OR a choice list), never both.
    * ``header`` and ``footer`` compose an interactive message, so each REQUIRES ``options`` or
      ``sections`` present.
    * ``template`` is the standalone out-of-window send: exclusive with every other content and
      interactive field (``media``, ``location``, ``options``, ``sections``, ``schema``; and
      transitively ``header``/``footer``, which require ``options``/``sections``).
    """
    if not message.strip():
        if not media and location is None:
            raise ValueError(f"message must be non-blank unless media or location carries the {noun} content")
        if options is not None or sections is not None:
            raise ValueError(f"a content-only (blank-message) {noun} carries no options; a choice needs a prompt")
        if schema is not None:
            raise ValueError(f"a content-only (blank-message) {noun} carries no schema; a form needs a prompt")
    if options is not None and sections is not None:
        raise ValueError(f"options and sections are mutually exclusive on one {noun}")
    if schema is not None and options is not None:
        raise ValueError(f"schema and options are mutually exclusive on one {noun}")
    if schema is not None and sections is not None:
        raise ValueError(f"schema and sections are mutually exclusive on one {noun}")
    if header is not None and options is None and sections is None:
        raise ValueError(f"a header requires options or sections on the {noun}")
    if footer is not None and options is None and sections is None:
        raise ValueError(f"a footer requires options or sections on the {noun}")
    if template is not None:
        for field, present in (
            ("media", media is not None),
            ("location", location is not None),
            ("options", options is not None),
            ("sections", sections is not None),
            ("schema", schema is not None),
            ("header", header is not None),
            ("footer", footer is not None),
        ):
            if present:
                raise ValueError(f"{field} and template are mutually exclusive on one {noun}")


with warnings.catch_warnings():
    # The ``schema`` field intentionally shadows pydantic's deprecated
    # ``BaseModel.schema()`` alias (the current API is ``model_json_schema()``);
    # the field name matches the JSON-schema payload it carries. Suppress the
    # shadow warning at the definition site so every importer is safe regardless
    # of its own warnings config — narrowly matched, never a blanket ignore.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class ChannelNotification(BaseModel):
        """One fire-and-forget message handed to a channel.

        A notification carries no interaction, no ticket, no ``callback_url`` and
        no deadline: the channel sends the message and nothing travels back.
        ``recipient`` is the OPTIONAL caller-requested address (chat id, phone
        number, ...): the channel plugin validates it against its operator-set
        allowlist and refuses to send to an unlisted address; when omitted the
        plugin sends to its operator-configured default recipient. It is an
        address only, never a secret or credential.

        ``sender_identity`` is the OPTIONAL address to send FROM when the channel fronts
        several operator identities: an internal routing control set by the sending side,
        never caller-supplied, and an address only — never a secret.

        ``message`` is the human-readable text, non-blank BY DEFAULT — EXCEPT it may be the empty
        string ``""`` for a CONTENT-ONLY send: a caption-less bubble that is just ``media`` or a
        ``location``, with no text carrier. The admissible states are "``message`` non-blank" OR
        "blank ``message`` WITH non-empty ``media`` OR a ``location``"; a blank ``message`` with no
        such content has nothing to deliver and is refused. (``message`` stays REQUIRED — a
        content-only sender passes ``""`` explicitly — because every caller constructs it in code
        with the text in hand.) An interactive surface (``options``, ``sections`` or ``schema``)
        REQUIRES a non-blank ``message`` — a choice or a form needs a prompt — so a content-only
        send carries none; a ``template`` likewise rides a non-blank ``message``.

        The OPTIONAL richer-send forms reuse the same ``message`` as the human-readable
        equivalent. ``media`` is display media the channel sends alongside the message (reusing
        :class:`MediaItem`, image/document/video/audio/link); a present list is non-empty.
        ``location`` is a shared geographic point (:class:`LocationElement`). ``template`` sends a
        pre-approved :class:`ChannelTemplate` for out-of-window delivery. ``options`` is a flat
        list of tappable :data:`Option` entries — a :class:`ReplyOption` (a tap submits its text
        as a visitor message) or a :class:`LinkOption` (a tap opens its url) — at most
        ``NOTIFICATION_OPTIONS_MAX``. ``sections`` is the sectioned alternative: titled
        :class:`OptionSection` groups of reply rows (rows summed across sections stay within
        ``NOTIFICATION_OPTIONS_MAX``). ``header`` is a single display-media header and ``footer`` a
        short trailing line, each composing an interactive message (they REQUIRE ``options`` or
        ``sections``).

        The composition rules (:func:`check_interactive_composition`): ``options`` XOR
        ``sections`` (one choice surface); ``schema`` excludes both (one interactive surface); a
        ``template`` is the standalone out-of-window send, MUTUALLY EXCLUSIVE with every other
        content and interactive field; ``options``/``sections`` and ``schema`` MAY each combine
        with ``media`` and ``location``. A channel that does not advertise the matching capability
        flag (``supports_media_notifications`` / ``supports_location_notifications`` /
        ``supports_template_notifications`` / ``supports_interactive_notifications`` /
        ``supports_form_notifications``, the OPTIONAL class-attribute convention documented on
        :class:`Channel`) never receives the matching field.

        ``schema`` is the form answer schema for an ASK-LESS FORM: the channel renders
        ``message`` as the form's prompt and ``schema`` as the fillable form, and the
        guest's submission enters the conversation as a guest message — no interaction,
        no ticket, no callback, the same inbound path a tapped option takes. A present
        ``schema`` is a non-empty dict; its deep shape is the sender's shared
        channel-deliverable subset walk (the same split :class:`ChannelDelivery` keeps),
        never re-checked here. ``schema`` REQUIRES a non-blank ``message`` — a form needs
        a prompt — and is MUTUALLY EXCLUSIVE with ``template`` and with ``options`` (one
        message carries ONE interactive surface); it MAY combine with ``media``. It rides
        the ``supports_form_notifications`` capability flag, and a form channel's OPTIONAL
        ``validate_form_schema(schema, question)`` hook (see :class:`Channel`) is reused
        at notify time with this ``message`` as the ``question`` argument, so the
        channel's own form limits refuse an unrenderable form before the send. Some channels
        also constrain WHEN a form may be sent: WhatsApp delivers a notify-form only inside
        the provider's customer-service window, and an out-of-window send fails loudly at the
        channel — never silently downgraded.
        """

        model_config = ConfigDict(frozen=True)

        message: str  # human-readable text; blank ("") ONLY for a content-only send (media/location carries it)
        recipient: str | None = None  # caller-requested address; None -> plugin default
        sender_identity: str | None = None  # internal sending identity; None -> plugin default
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
            # Length cap only; whether a BLANK message is admissible depends on ``media`` (a
            # media-only send carries no text) and is decided in :meth:`_message_or_media` once
            # every field is bound.
            if len(value) > NOTIFICATION_MESSAGE_MAX_CHARS:
                raise ValueError(
                    f"message must be at most {NOTIFICATION_MESSAGE_MAX_CHARS} characters, got {len(value)}"
                )
            return value

        @field_validator("recipient", "sender_identity")
        @classmethod
        def _address_non_empty(cls, value: str | None) -> str | None:
            if value is not None and not value.strip():
                raise ValueError("must be a non-empty address when present")
            if value is not None and len(value) > NOTIFICATION_ADDRESS_MAX_CHARS:
                raise ValueError(
                    f"address must be at most {NOTIFICATION_ADDRESS_MAX_CHARS} characters, got {len(value)}"
                )
            return value

        @field_validator("media")
        @classmethod
        def _check_media(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
            # None means no media; the shared list-level caps (non-empty, item count, summed
            # URI) are the same wire-contract bound on the notify REQUEST as on the ask
            # REQUEST — the channel refuses anything beyond its own native envelope.
            if value is not None:
                check_media_list(value)
            return value

        @field_validator("options")
        @classmethod
        def _options_valid(cls, value: list[Option] | None) -> list[Option] | None:
            # None means no options; a present flat list is non-empty and capped. Each option's own
            # shape (reply text / link label+url) is the ReplyOption/LinkOption concern.
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
            # None means no form; a present schema is a non-empty dict — the same bound the
            # ask-path ChannelDelivery enforces. The deep shape is the sender's shared
            # channel-deliverable subset walk, never re-implemented here.
            if value is not None and not value:
                raise ValueError("schema must be a non-empty dict when present")
            return value

        @model_validator(mode="after")
        def _check_composition(self) -> ChannelNotification:
            # The shared cross-field interactive-composition rules, so this carrier and AnswerPart
            # can never drift (see :func:`check_interactive_composition`).
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
                noun="notification",
            )
            return self


@runtime_checkable
class Channel(Protocol):
    """Delivers one question to a human on a specific medium.

    A channel plugin registers an instance under a name
    (``tai42_app.channels.register``); ``ask_user`` resolves it by name and calls
    ``deliver`` after the interaction is persisted and its callback ticket is
    minted. A channel never reaches the interactions store directly: the
    human's reply travels back through the delivery's public ``callback_url``.

    A channel MAY advertise richer support with six OPTIONAL, class-level
    capability flags — ``supports_media_notifications``,
    ``supports_template_notifications``, ``supports_interactive_notifications``,
    ``supports_location_notifications``, ``supports_form_notifications`` (all
    five for ``notify``) and ``supports_form_delivery`` (for ``deliver``) — set
    as plain class
    attributes. They are a documented convention, NOT Protocol members: a
    channel that supports the richer form sets the matching attribute to
    ``True``; a channel that omits it advertises no support (absent =
    ``False``). Because they are not part of the Protocol, a text-only channel
    that declares none is still a valid ``Channel`` (both structurally and under
    runtime ``isinstance``). The ask/notify helpers read them defensively with
    ``getattr(channel, "<flag>", False)`` and refuse the matching richer send to
    a channel that does not advertise the flag: ``notify_user`` refuses a media,
    template, options, sections, location or schema notification, and the
    ``ask_user`` helper
    refuses a ``form`` delivery, to a channel without the flag — so a channel
    that reads only the plain fields can never silently drop the extra content.
    A channel that does not advertise ``supports_form_delivery`` never receives
    a ``form`` delivery, and one that does not advertise
    ``supports_form_notifications`` never receives a ``schema`` notification.

    A form channel MAY also declare one OPTIONAL method, ``validate_form_schema``,
    following the same convention as the capability flags — a documented member,
    NOT a Protocol method, so declaring it never tightens the runtime structural
    check. ``ask_user`` reads it defensively with
    ``getattr(channel, "validate_form_schema", None)`` right after the generic
    channel-deliverable subset check and, when present, calls
    ``channel.validate_form_schema(schema, question)`` at ask-time, BEFORE any
    state is written. It enforces the channel's OWN ask-time-knowable form limits
    (reserved property names, per-medium Block Kit / Flow caps, question-text caps)
    over the schema AND the question text — limits the generic subset does not
    know — raising ``ValueError`` naming the offending property/limit on a
    violation, so a question or schema the channel could never render is refused up
    front instead of persisting a question that only fails at delivery. A channel
    that omits it advertises no extra ask-time limits; its delivery path still
    refuses an unrenderable question or schema (a permanent
    :class:`ChannelInputError`). The SAME hook is reused for a form notification:
    the notify helper calls ``channel.validate_form_schema(schema, message)`` with
    the notification's ``message`` as the ``question`` argument before the send, so
    one declared method covers both the ask and the notify form surfaces.

    A channel MAY also declare one OPTIONAL method, ``deliver_ordered(notifications)``,
    for a NATIVE in-order batch (a bulk API, a transactional transcript append) — the
    same documented-member convention as ``validate_form_schema`` and the capability
    flags, NOT a Protocol method (so declaring it never tightens the runtime structural
    check and a channel that omits it stays a valid ``Channel``). It takes a
    ``Sequence[ChannelNotification]`` and returns ``list[list[str]]`` — the per-message
    ids in send order, one list per notification — sending strictly in order and never
    reordering, skipping or parallelising; the FIRST failure raises
    :class:`ChannelDeliveryError` / :class:`ChannelInputError`, with the accepted ids
    named in the exception message (as the WhatsApp body-then-media send does). A caller
    reaches the default sequential behaviour through :func:`notify_in_order`, which
    dispatches to ``deliver_ordered`` when declared and otherwise loops ``notify``; a
    channel that declares neither still delivers a batch one ``notify`` at a time.
    """

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Push ``delivery`` to the medium, or raise :class:`ChannelDeliveryError`.

        Send the question to the resolved recipient — ``delivery.recipient``
        when set (after checking it against the plugin's operator allowlist),
        else the plugin's operator-configured default — and arrange for
        the reply to reach ``delivery.callback_url`` — either a tappable link
        carrying the URL, or an inbound-route correlation the plugin stores.
        Any delivery failure — an unreachable or rejecting medium, a recipient
        outside the operator allowlist, a required credential or recipient not
        configured, a bad send response — raises
        :class:`ChannelDeliveryError`; a plain return is the only success
        signal. One send attempt only: retrying is the caller's decision, never
        an implicit loop here — the raised error's ``retryable`` and
        ``retry_after`` drive that decision, so a fault the medium can recover
        from is classified rather than blind-retried here.
        """
        ...

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send a fire-and-forget message, or raise :class:`ChannelDeliveryError`.

        No interaction, no ticket, no callback, no reply. Any delivery failure raises
        :class:`ChannelDeliveryError`; a permanent refusal of the input's shape or
        content (an input the medium cannot render BY NATURE) raises
        :class:`ChannelInputError` instead — retrying it cannot succeed. A return means
        the medium ACCEPTED the message —
        not that a human saw it — and yields the per-message ids it assigned this send
        (several when the medium splits a long message, empty when it exposes no id),
        which later correlate an out-of-band delivery receipt back to this send. One
        send attempt only, no retry. A channel that cannot notify raises
        :class:`NotImplementedError`.
        """
        raise NotImplementedError


async def notify_in_order(
    channel: Channel,
    notifications: Sequence[ChannelNotification],
    *,
    on_sent: Callable[[int, list[str]], None] | None = None,
) -> list[list[str]]:
    """Deliver ``notifications`` to ``channel`` STRICTLY in order, returning the
    per-message ids of each (one ``list[str]`` per notification, in the same order).

    The default sequential in-order primitive every channel "inherits" by a caller using
    this helper — the place a Protocol default can actually reach a structural
    implementer. When the channel declares the OPTIONAL ``deliver_ordered`` member (read
    defensively with ``getattr``, the platform's established convention for optional
    channel abilities) the batch is handed to it as a native ordered send; otherwise each
    notification is delivered with one awaited ``notify`` before the next goes out. Either
    way delivery is never reordered, never parallelised and never skipped, and it STOPS at
    the first raise (:class:`ChannelDeliveryError` / :class:`ChannelInputError`) — the
    caller learns how far the sequence got from ``on_sent`` (and, for a native batch, from
    the exception naming the accepted ids).

    ``on_sent(index, ids)`` is the progress hook a caller uses to record each accepted send
    (a send ledger, say); it fires once per notification in send order. It is intentionally
    the ONLY progress seam — a caller that needs work BETWEEN sends (a per-send lease refresh)
    drives ``notify`` itself rather than routing through this helper, which cannot express a
    pre-send hook.

    Two honesty caveats a durable caller must weigh before relying on ``on_sent``:

    - This helper is NOT yet wired into the conversations delivery machine (which chunks and
      ledgers each send inline in :mod:`tai42_skeleton.conversations.delivery`). It is the
      documented in-order primitive, not the code path a durable ordered answer currently
      flows through.
    - Per-send timing holds ONLY on the sequential (``notify``-loop) path, where ``on_sent``
      fires AFTER each accepted send and BEFORE the next goes out. On the native
      ``deliver_ordered`` path the whole batch is sent inside that one call and ``on_sent``
      fires per index only AFTER it returns — so a durable caller that needs per-send
      ledgering interleaved with the sends must NOT rely on ``on_sent`` there; it should ledger
      inline (as the conversations machine does) rather than through this helper.
    """
    ordered = list(notifications)
    native = getattr(channel, "deliver_ordered", None)
    if callable(native):
        # ``deliver_ordered`` is a documented OPTIONAL member, not a Protocol method, so it
        # is read off the instance untyped; cast it to its documented signature.
        ordered_send = cast("Callable[[Sequence[ChannelNotification]], Awaitable[list[list[str]]]]", native)
        results: list[list[str]] = await ordered_send(ordered)
        if on_sent is not None:
            for index, ids in enumerate(results):
                on_sent(index, ids)
        return results
    results = []
    for index, notification in enumerate(ordered):
        ids = await channel.notify(notification)
        results.append(ids)
        if on_sent is not None:
            on_sent(index, ids)
    return results


class Correlation(BaseModel):
    """The per-address record a channel keeps while ONE parked ask awaits the
    guest's next inbound reply.

    When ``ask_user`` is delivered on a medium whose reply arrives as a fresh
    inbound message (not a tap on a signed link), the channel stores this record
    against a channel-computed correlation key and, when the guest's next reply
    lands on that key, forwards it to ``callback_url`` (the delivery's public
    answer sink). ``interaction_id`` identifies the parked ask (carried into
    operator alerts, never re-derived); ``ttl_deadline`` is the tz-aware instant
    past which the pending ask is stale and the key may be reclaimed. One pending
    ask per address: a channel reserves the key before delivering and drops it
    once the reply is forwarded, withdrawn or expired.
    """

    model_config = ConfigDict(frozen=True)

    callback_url: str  # the delivery's public /api/interactions/callback/{ticket} answer sink
    interaction_id: str  # the parked ask this record awaits a reply for
    ttl_deadline: datetime  # tz-aware; past it the pending ask is stale and the key reclaimable
    # The parked ask's digression policy the shared answer ladder reads at a 400 rejection:
    # ``retry`` (keep + notify, today's behavior) or ``bridge`` (keep + bridge the reply as a
    # fresh turn, no notice). A channel copies it from the ``ChannelDelivery`` it delivered;
    # a channel that does not set it keeps the safe default.
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY
    # The ask's custom retry-notice text the ladder sends instead of the built-in one (``retry``
    # policy only); ``None`` uses the built-in notice. Carried from the ``ChannelDelivery``.
    mismatch_notice: str | None = None

    @field_validator("ttl_deadline")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # A naive deadline compared against an aware ``now()`` raises TypeError at
        # use time; reject it here and normalize to UTC (same strictness as
        # ChannelDelivery.timeout_at).
        if value.tzinfo is None:
            raise ValueError("ttl_deadline must be timezone-aware (UTC)")
        return value.astimezone(UTC)


@runtime_checkable
class CorrelationStore(Protocol):
    """Storage primitives ONLY for the one-pending-per-address correlation record —
    no policy.

    A channel that delivers ``ask_user`` questions whose replies arrive as fresh
    inbound messages keeps a :class:`Correlation` per waiting address so the
    guest's next reply resolves the right parked ask. This port is the minimal
    set/get/release surface over that store; the LADDER that interprets a
    forwarded answer's outcome (forward, retry-in-place, bridge) lives in core and
    reads this port — it is not the store's concern.

    ``key`` is an OPAQUE channel-computed correlation key: it absorbs the divergent
    per-channel shapes — a Twilio number pair, a Slack ``thread_ts``, a Telegram
    ForceReply ``message_id``, a WhatsApp address — collapsing them to one string
    the store never interprets. A channel with no correlated replies (a link-tap
    or an external-answer medium) simply does not provide a store.

    This is a STANDALONE optional port, NOT an extension of :class:`Channel`: a
    channel implements it separately (or not at all), and the core handler is
    handed one explicitly rather than reaching it off the channel instance.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` with a ``ttl_seconds`` expiry, NX.

        Returns True when the key was free and is now held; False when the key is
        already held — the one-pending-per-address guarantee, so a second parked
        ask for the same address never silently overwrites the first. ``ttl_seconds``
        bounds how long the reservation survives without a reply.
        """
        ...

    async def get_correlation(self, key: str) -> Correlation | None:
        """Return the record held under ``key``, or ``None`` when none is held.

        A non-destructive peek: it neither drops nor refreshes the reservation, so
        the handler can inspect the pending ask and decide the outcome before
        releasing.
        """
        ...

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation held under ``key``, idempotently.

        A no-op when the key is already free (expired, forwarded, or never held),
        so releasing twice — or racing an expiry — is never an error.
        """
        ...


class AnswerForwardError(Exception):
    """The interactions answer door did not accept a forwarded answer on a status the
    shared inbound-answer ladder cannot resolve (401/413/5xx or a transport fault).

    Raised by :meth:`AppChannels.handle_inbound_answer` WITHOUT releasing the
    correlation, so the channel's transport-level retry (the provider's webhook
    redelivery) re-runs the ladder and the answer is never silently lost. A channel
    lets it propagate out of its inbound webhook so the provider redelivers — the
    same loud-failure contract each channel kept when it hand-rolled the ladder.
    """


class InboundAnswerOutcome(StrEnum):
    """What the shared inbound-answer ladder decided for one inbound reply on a
    correlation key. A channel maps this to its own transport ack."""

    NO_CORRELATION = "no_correlation"  # no pending ask on this key — the CALLER bridges it as a normal turn
    FORWARDED = "forwarded"  # the door accepted the answer; the correlation was released
    RETRY_KEPT = "retry_kept"  # the door rejected a re-answerable ask; correlation KEPT, guest told what's expected
    BRIDGED = "bridged"  # the ask is gone or the mismatch is hard; correlation released and the reply bridged
    # A ``bridge``-policy ask rejected a reply: the correlation is KEPT (the ask stays parked) and
    # the reply is bridged as a fresh turn with NO guest notice — the reply was a digression.
    BRIDGED_KEPT = "bridged_kept"


class InboundBridge(BaseModel):
    """The context a bridged turn needs when a reply is not (or no longer) an answer.

    A channel hands one of these to :meth:`AppChannels.handle_inbound_answer` alongside
    the correlation key and answer value. ``channel_id`` is the registered channel
    name; ``our_identity`` and ``client_address`` are the conversation's two addresses
    (the operator identity the turn answers from, and the guest's attested address /
    thread); ``cap_key`` is the party the per-address turn cap holds accountable;
    ``provider_message_id`` dedupes a provider redelivery at the conversation seam;
    ``bridge_text`` is the channel's faithful rendering of the guest's message for a
    bridged turn.

    ``owns_retry_notice`` lets a channel OWN the guest-facing correction message on a
    retryable rejection. The default (False) is that the ladder sends the generic
    "that didn't match, try again" notice on :attr:`InboundAnswerOutcome.RETRY_KEPT`.
    When True, the channel's correction surface IS a re-ask the channel renders off
    RETRY_KEPT (a re-opened WhatsApp Flow, a Slack modal's inline Block-Kit error), so
    the ladder SKIPS its notice to avoid double-messaging — it still keeps the
    correlation and still emits the operator event (tagged ``notice_owner="channel"``).
    It applies ONLY to the retryable path: on a hard mismatch (a closed ask) the
    channel's re-ask surface is moot, so the ladder always sends the final "question is
    closed" notice regardless of this flag.

    ``params`` is the OPTIONAL opaque channel enrichment this inbound reply carries — the
    ANSWER-path counterpart of a conversation entry's ``params``: a tapped reply id, a template
    button payload, a referral, the reply-to context the guest quoted. The ladder threads it BOTH
    ways with the same seam symmetry — forwarded to the ask's callback door alongside the answer
    (landing on :class:`~tai42_contract.interactions.models.InteractionResponse.params`, read by
    the asking flow beside ``answer``) AND, when the reply is instead BRIDGED as a fresh turn,
    passed to ``accept`` as its ``params`` — so enrichment is never dropped on either arm. The
    SAME transport vocabulary (:func:`~tai42_contract.entry_params.validate_entry_params`) bounds
    it; the platform attaches no meaning and NO TRUST. ``None`` means no enrichment.
    """

    model_config = ConfigDict(frozen=True)

    channel_id: str
    our_identity: str
    client_address: str
    cap_key: str
    provider_message_id: str
    bridge_text: str
    owns_retry_notice: bool = False
    params: dict[str, str] | None = None

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return validate_entry_params(value)


class InboundAnswerResult(BaseModel):
    """The result of one inbound-answer ladder run.

    ``outcome`` is the ladder's decision. ``retry_reason`` and ``retry_field`` carry the
    door's OWN (already length-bounded) rejection message and the failing field name so a
    channel that OWNS its correction surface (a re-opened WhatsApp Flow, a Slack modal's
    inline Block-Kit error) can render the door's SPECIFIC message rather than a generic
    line. Both are populated when the door rejected the answer's content — on
    :attr:`InboundAnswerOutcome.RETRY_KEPT` (either ``notice_owner`` variant) and on a
    hard-mismatch :attr:`InboundAnswerOutcome.BRIDGED` — and are ``None`` on every other
    outcome (no correlation, a clean forward, a gone-ask 404 bridge). A channel that
    renders no correction of its own simply ignores them and maps ``outcome``.
    """

    model_config = ConfigDict(frozen=True)

    outcome: InboundAnswerOutcome
    retry_reason: str | None = None
    retry_field: str | None = None


__all__ = [
    "NOTIFICATION_FOOTER_MAX_CHARS",
    "NOTIFICATION_SECTIONS_MAX",
    "OPTION_ID_MAX_CHARS",
    "TEMPLATE_BUTTONS_MAX",
    "TEMPLATE_PARAM_MAX_CHARS",
    "AnswerForwardError",
    "Channel",
    "ChannelDelivery",
    "ChannelDeliveryError",
    "ChannelInputError",
    "ChannelNotification",
    "ChannelTemplate",
    "Correlation",
    "CorrelationStore",
    "InboundAnswerOutcome",
    "InboundAnswerResult",
    "InboundBridge",
    "LinkOption",
    "Option",
    "OptionSection",
    "QuickReplyButtonParam",
    "ReplyOption",
    "TemplateButtonParam",
    "UrlButtonParam",
    "check_footer",
    "check_header",
    "check_interactive_composition",
    "check_options",
    "check_sections",
    "notify_in_order",
]
