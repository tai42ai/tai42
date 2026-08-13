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
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.interactions.models import AnswerFormat, MediaItem


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

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


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
        options: list[str] | None = None  # present for select
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

        @model_validator(mode="after")
        def _check_options(self) -> ChannelDelivery:
            if self.answer_format == AnswerFormat.SELECT:
                if not self.options:
                    raise ValueError("select answer_format requires non-empty options")
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

    ``media`` and ``template`` are OPTIONAL richer-send forms reusing the same
    ``message`` as the human-readable equivalent. ``media`` is display media the
    channel sends alongside the message (reusing :class:`MediaItem`); a present
    list is non-empty. ``template`` sends a pre-approved :class:`ChannelTemplate`
    for out-of-window delivery. The two are MUTUALLY EXCLUSIVE — a template is the
    out-of-window send and a companion standalone media item would be rejected by
    the medium there, so setting both is refused rather than partially delivered.
    A channel that does not advertise the matching capability flag
    (``supports_media_notifications`` / ``supports_template_notifications``, the
    OPTIONAL class-attribute convention documented on :class:`Channel`) never
    receives these fields.
    """

    model_config = ConfigDict(frozen=True)

    message: str
    recipient: str | None = None  # caller-requested address; None -> plugin default
    sender_identity: str | None = None  # internal sending identity; None -> plugin default
    media: list[MediaItem] | None = None  # display media sent WITH the message; None -> none
    template: ChannelTemplate | None = None  # out-of-window template send; None -> freeform

    @field_validator("message")
    @classmethod
    def _message_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must be non-blank")
        return value

    @field_validator("recipient", "sender_identity")
    @classmethod
    def _address_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must be a non-empty address when present")
        return value

    @field_validator("media")
    @classmethod
    def _media_non_empty(cls, value: list[MediaItem] | None) -> list[MediaItem] | None:
        # None means no media; a present-but-empty list is a caller bug.
        if value is not None and not value:
            raise ValueError("media must be a non-empty list when present")
        return value

    @model_validator(mode="after")
    def _media_template_exclusive(self) -> ChannelNotification:
        if self.media is not None and self.template is not None:
            raise ValueError("media and template are mutually exclusive on one notification")
        return self


@runtime_checkable
class Channel(Protocol):
    """Delivers one question to a human on a specific medium.

    A channel plugin registers an instance under a name
    (``tai42_app.channels.register``); ``ask_user`` resolves it by name and calls
    ``deliver`` after the interaction is persisted and its callback ticket is
    minted. A channel never reaches the interactions store directly: the
    human's reply travels back through the delivery's public ``callback_url``.

    A channel MAY advertise richer support with three OPTIONAL, class-level
    capability flags — ``supports_media_notifications``,
    ``supports_template_notifications`` (both for ``notify``) and
    ``supports_form_delivery`` (for ``deliver``) — set as plain class
    attributes. They are a documented convention, NOT Protocol members: a
    channel that supports the richer form sets the matching attribute to
    ``True``; a channel that omits it advertises no support (absent =
    ``False``). Because they are not part of the Protocol, a text-only channel
    that declares none is still a valid ``Channel`` (both structurally and under
    runtime ``isinstance``). The ask/notify helpers read them defensively with
    ``getattr(channel, "<flag>", False)`` and refuse the matching richer send to
    a channel that does not advertise the flag: ``notify_user`` refuses a media
    or template notification, and the ``ask_user`` helper refuses a ``form``
    delivery, to a channel without the flag — so a channel that reads only the
    plain fields can never silently drop the extra content. A channel that does
    not advertise ``supports_form_delivery`` never receives a ``form`` delivery.
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
        :class:`ChannelDeliveryError`; a return means the medium ACCEPTED the message —
        not that a human saw it — and yields the per-message ids it assigned this send
        (several when the medium splits a long message, empty when it exposes no id),
        which later correlate an out-of-band delivery receipt back to this send. One
        send attempt only, no retry. A channel that cannot notify raises
        :class:`NotImplementedError`.
        """
        raise NotImplementedError


__all__ = [
    "Channel",
    "ChannelDelivery",
    "ChannelDeliveryError",
    "ChannelNotification",
    "ChannelTemplate",
]
