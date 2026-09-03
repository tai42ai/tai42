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
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.errors import ErrorKind
from tai42_contract.interactions.models import (
    MEDIA_CAPTION_MAX_CHARS,
    AnswerFormat,
    MediaItem,
    check_media_list,
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
        media: list[MediaItem] | None = None  # display media rendered WITH the question; None -> none
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


class ChannelTemplate(BaseModel):
    """A pre-approved, named template a channel sends outside its freeform window.

    Some media only accept arbitrary text inside a bounded conversation window
    (e.g. WhatsApp's 24-hour customer-service window); outside it, the sole
    accepted send is an operator-authored template referenced by ``name`` in an
    approved ``language`` — both required and non-blank. ``parameters`` is the
    POSITIONAL list of body-text values substituted into the template's body
    placeholders in order; it is empty when the template has no placeholders.
    Body text only — templates with header media, button parameters, or typed
    parameters (currency, date-time) are out of scope for this contract.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    language: str
    parameters: list[str] = Field(default_factory=list)

    @field_validator("name", "language")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-blank")
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
        string ``""`` for a MEDIA-ONLY send: a caption-less image or other bubble that is just
        ``media``, with no text carrier. The admissible states are "``message`` non-blank" OR
        "blank ``message`` WITH non-empty ``media``"; a blank ``message`` and no media has nothing
        to deliver and is refused. (``message`` stays REQUIRED — a media-only sender passes ``""``
        explicitly — because every caller constructs it in code with the text in hand.) ``options``
        REQUIRE a non-blank ``message`` — a tappable choice needs a prompt — so a media-only send
        carries none; a ``template`` likewise rides a non-blank ``message`` (it is not ``media``, so
        a blank message with only a template is refused).

        ``media``, ``template`` and ``options`` are OPTIONAL richer-send forms reusing the
        same ``message`` as the human-readable equivalent. ``media`` is display media the
        channel sends alongside the message (reusing :class:`MediaItem`); a present
        list is non-empty. ``template`` sends a pre-approved :class:`ChannelTemplate`
        for out-of-window delivery. ``options`` is a list of tappable options; a tap enters
        the conversation as a visitor message on channels that support it — a present list is
        non-empty, each option non-blank and at most ``NOTIFICATION_OPTION_MAX_CHARS`` characters,
        at most ``NOTIFICATION_OPTIONS_MAX`` entries.
        ``media`` and ``template`` are MUTUALLY EXCLUSIVE — a template is the out-of-window
        send and a companion standalone media item would be rejected by the medium there, so
        setting both is refused rather than partially delivered; ``options`` is likewise
        incompatible with ``template`` but MAY combine with ``media`` (a card with a list).
        A channel that does not advertise the matching capability flag
        (``supports_media_notifications`` / ``supports_template_notifications`` /
        ``supports_interactive_notifications`` / ``supports_form_notifications``, the
        OPTIONAL class-attribute convention documented on :class:`Channel`) never receives
        the matching field.

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
        channel's own form limits refuse an unrenderable form before the send. Some media
        bound WHEN a form may be sent: WhatsApp delivers a notify-form only inside the
        provider's customer-service window, and an out-of-window send fails loudly at the
        channel — never silently downgraded.
        """

        model_config = ConfigDict(frozen=True)

        message: str  # human-readable text; blank ("") ONLY for a media-only send (media carries it)
        recipient: str | None = None  # caller-requested address; None -> plugin default
        sender_identity: str | None = None  # internal sending identity; None -> plugin default
        media: list[MediaItem] | None = None  # display media sent WITH the message; None -> none
        template: ChannelTemplate | None = None  # out-of-window template send; None -> freeform
        options: list[str] | None = None  # tappable options; a tap enters the conversation; None -> none
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
        def _options_valid(cls, value: list[str] | None) -> list[str] | None:
            # None means no options; a present list is non-empty, each option non-blank,
            # and capped so one notification cannot fan out an unbounded tap list.
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
        def _message_or_media(self) -> ChannelNotification:
            # A message is either non-blank text OR a media-only send: a blank message carried by
            # non-empty media (a caption-less image). A blank message with no media has nothing to
            # deliver and is refused. Options REQUIRE a non-blank message — a tappable choice needs
            # a prompt — and a schema does too — a form needs a prompt — so a media-only send
            # carries neither. A template is not media, so a blank message with only a template is
            # refused here too (its ``not self.media`` branch).
            if not self.message.strip():
                if not self.media:
                    raise ValueError("message must be non-blank unless media carries the content")
                if self.options is not None:
                    raise ValueError(
                        "media-only (blank-message) notification carries no options; a choice needs a prompt"
                    )
                if self.schema is not None:
                    raise ValueError("media-only (blank-message) notification carries no schema; a form needs a prompt")
            return self

        @model_validator(mode="after")
        def _media_template_exclusive(self) -> ChannelNotification:
            if self.media is not None and self.template is not None:
                raise ValueError("media and template are mutually exclusive on one notification")
            return self

        @model_validator(mode="after")
        def _options_template_exclusive(self) -> ChannelNotification:
            if self.options is not None and self.template is not None:
                raise ValueError("options and template are mutually exclusive on one notification")
            return self

        @model_validator(mode="after")
        def _schema_template_exclusive(self) -> ChannelNotification:
            if self.schema is not None and self.template is not None:
                raise ValueError("schema and template are mutually exclusive on one notification")
            return self

        @model_validator(mode="after")
        def _schema_options_exclusive(self) -> ChannelNotification:
            # One message carries ONE interactive surface: a form's fields or a tap list, never both.
            if self.schema is not None and self.options is not None:
                raise ValueError("schema and options are mutually exclusive on one notification")
            return self


@runtime_checkable
class Channel(Protocol):
    """Delivers one question to a human on a specific medium.

    A channel plugin registers an instance under a name
    (``tai42_app.channels.register``); ``ask_user`` resolves it by name and calls
    ``deliver`` after the interaction is persisted and its callback ticket is
    minted. A channel never reaches the interactions store directly: the
    human's reply travels back through the delivery's public ``callback_url``.

    A channel MAY advertise richer support with five OPTIONAL, class-level
    capability flags — ``supports_media_notifications``,
    ``supports_template_notifications``, ``supports_interactive_notifications``,
    ``supports_form_notifications`` (all four for ``notify``) and
    ``supports_form_delivery`` (for ``deliver``) — set as plain class
    attributes. They are a documented convention, NOT Protocol members: a
    channel that supports the richer form sets the matching attribute to
    ``True``; a channel that omits it advertises no support (absent =
    ``False``). Because they are not part of the Protocol, a text-only channel
    that declares none is still a valid ``Channel`` (both structurally and under
    runtime ``isinstance``). The ask/notify helpers read them defensively with
    ``getattr(channel, "<flag>", False)`` and refuse the matching richer send to
    a channel that does not advertise the flag: ``notify_user`` refuses a media,
    template, options or schema notification, and the ``ask_user`` helper
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
    """

    model_config = ConfigDict(frozen=True)

    channel_id: str
    our_identity: str
    client_address: str
    cap_key: str
    provider_message_id: str
    bridge_text: str
    owns_retry_notice: bool = False


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
    "notify_in_order",
]
