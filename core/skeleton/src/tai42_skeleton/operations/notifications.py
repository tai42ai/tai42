"""Notifications operations — read the internal inbox and send a notification.

* ``list_notifications`` returns the deployment's internal notifications feed —
  channel-less sends plus any audience-addressed notification (recorded even when
  a channel also delivers it) — newest-first.
* ``notify_user`` sends a human ONE fire-and-forget message — on a named channel,
  or (channel omitted) recorded to the internal notifications sink the Studio inbox
  reads. No reply, no blocking wait. It delivers over the channels feature's
  ``notify_user`` helper, mapping the helper's loud
  failures to the operation's typed errors: a blank message with no media to carry
  it / unknown channel /
  blank recipient/audience — or a caller-supplied ``sender_identity`` (an
  internal-only control) — or a channel's permanent refusal of the input's shape
  (a :class:`ChannelInputError`, retrying cannot succeed) — is a
  :class:`BadRequestError` (400), a restricted caller
  addressing another identity is a cross-identity authorization denial mapped to a
  :class:`ForbiddenError` (403) — the same 403 the read-side answer door raises for the
  symmetric read denial — a channel that cannot notify
  is a :class:`NotSupportedError` (501), and a channel delivery failure splits on the
  raised error's ``retryable``: a transient one is a :class:`UnavailableError` (503)
  carrying the medium's ``retry_after`` when present, a permanent refusal is an
  :class:`UpstreamError` (502) — a failure is never swallowed.

``notify_user`` causes an external side-effect (a message leaves the deployment),
so it is ``destructive=True``. It is a messaging door, not a privilege-shaping one,
so it is NOT authority-changing and stays a plain (includable) projected tool.
"""

from __future__ import annotations

import warnings
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelTemplate,
    Option,
    OptionSection,
    check_footer,
    check_header,
    check_interactive_composition,
    check_sections,
)
from tai42_contract.interactions.models import LocationElement, MediaItem

from tai42_skeleton.access_control.user import CrossIdentityAudienceError, request_identity
from tai42_skeleton.channels.notifications_sink import read_notifications
from tai42_skeleton.channels.notify import SenderIdentityNotAllowedError
from tai42_skeleton.channels.notify import notify_user as _notify_user
from tai42_skeleton.interactions.settings import interactions_store_configured
from tai42_skeleton.operations import (
    BadRequestError,
    ForbiddenError,
    NotSupportedError,
    UnavailableError,
    UpstreamError,
    operation,
)

with warnings.catch_warnings():
    # The ``schema`` field intentionally shadows pydantic's deprecated
    # ``BaseModel.schema()`` alias (the current API is ``model_json_schema()``);
    # the field name matches the wire key and the helper keyword it maps to
    # (``model_dump`` feeds the operation's kwargs by FIELD NAME, so an aliased
    # spelling would break the mapping). Suppressed at the definition site,
    # narrowly matched — the same pattern the contract's ``ChannelNotification``
    # uses for its own ``schema`` field.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class NotifyUser(BaseModel):
        """A notification to send: the ``message`` text, an optional named ``channel``
        that carries it (omit to record to the internal sink), an optional per-call
        ``recipient`` delivery address, and an optional ``audience`` identity whose in-app
        inbox shows it (honored even with a channel set; distinct from ``recipient``)."""

        message: str
        channel: str | None = None
        recipient: str | None = None
        audience: str | None = Field(
            default=None,
            description=(
                "The identity (user_id) whose in-app inbox shows this (honored even with a channel set); "
                "leave unset for an operator/broadcast notification. Distinct from recipient, which is a "
                "channel delivery address."
            ),
        )
        sender_identity: str | None = Field(
            default=None,
            description=(
                "Reserved: the sending identity a channel message leaves FROM, set by the conversation "
                "bridge on the notification it builds to answer a route. Callers MUST NOT supply it here — "
                "a set value is rejected with a 400, never forwarded."
            ),
        )
        media: list[MediaItem] | None = Field(
            default=None,
            description=(
                "Optional display media sent WITH the message. On a named channel it requires a channel that "
                "advertises media support (else a 501); with no channel it is stored on the internal inbox "
                "record and rendered there. Mutually exclusive with template."
            ),
        )
        location: LocationElement | None = Field(
            default=None,
            description=(
                "Optional shared geographic point sent WITH the message (a map pin). On a named channel it "
                "requires a channel that advertises location support (else a 501); with no channel it is stored "
                "on the internal inbox record. It may carry the message content on its own (a blank message is "
                "admissible when media or a location carries it). Mutually exclusive with template."
            ),
        )
        template: ChannelTemplate | None = Field(
            default=None,
            description=(
                "Optional pre-approved template for an out-of-window send. On a named channel it requires a "
                "channel that advertises template support (else a 501); with no channel it is stored on the "
                "internal inbox record. Mutually exclusive with media and options."
            ),
        )
        options: list[Option] | None = Field(
            default=None,
            description=(
                "Optional FLAT tappable options sent WITH the message — each a reply option (a tap submits its "
                "text as a visitor message) or a link option (a tap opens its url). On a named channel it "
                "requires a channel that advertises interactive support (else a 501); with no channel it is "
                "stored on the internal inbox record. One message carries ONE interactive surface: mutually "
                "exclusive with template, sections and schema; may combine with media and location."
            ),
        )
        sections: list[OptionSection] | None = Field(
            default=None,
            description=(
                "Optional SECTIONED tappable options — titled groups of reply rows, the sectioned alternative to "
                "the flat options list (rows summed across sections stay within the same cap). On a named channel "
                "it requires a channel that advertises interactive support (else a 501); with no channel it is "
                "stored on the internal inbox record. One message carries ONE interactive surface: mutually "
                "exclusive with options, template and schema; may combine with media and location."
            ),
        )
        header: MediaItem | None = Field(
            default=None,
            description=(
                "Optional single display-media header above an interactive message. It COMPOSES an interactive "
                "message, so it REQUIRES options or sections; it rides the interactive choice surface's own "
                "capability (no separate flag) and mutually exclusive with template."
            ),
        )
        footer: str | None = Field(
            default=None,
            description=(
                "Optional short trailing line under an interactive message. Like the header it COMPOSES an "
                "interactive message, so it REQUIRES options or sections; it rides the choice surface's own "
                "capability (no separate flag) and mutually exclusive with template."
            ),
        )
        schema: dict[str, Any] | None = Field(  # pyright: ignore[reportIncompatibleMethodOverride]
            default=None,
            description=(
                "Optional form answer schema for an ask-less form: the channel renders the message as the "
                "form's prompt and this schema as the fillable form, and a submission enters the conversation "
                "as a message from the person. Requires a named channel (else a 400 — the internal sink has no "
                "submission door) that advertises form support (else a 501). Mutually exclusive with template "
                "and with options, may combine with media."
            ),
        )

        # The new rich fields reuse AnswerPart's/ChannelNotification's SAME field validators, so
        # the operator send surface bounds each shape identically to the flow answer path — never
        # a second, drift-prone rule set.
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

        @model_validator(mode="after")
        def _check_composition(self) -> NotifyUser:
            # The SHARED cross-field composition matrix every option-carrying carrier enforces
            # (ChannelNotification, AnswerPart), so the operator send model and the delivered
            # notification can never diverge — reused, never duplicated. The channels helper
            # re-validates by constructing the ChannelNotification, but enforcing it HERE gives the
            # operator door a clean 400 at the request boundary.
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


@operation(summary="List internal notifications", tags=["notifications"])
async def list_notifications() -> dict:
    """List internal notifications, newest-first.

    A RESTRICTED caller reads its OWN per-identity feed (complete within its own
    bound — never truncated by other identities' volume, never a broadcast); an
    UNRESTRICTED caller reads the shared feed unchanged (today's operator view)."""
    # OFF gate: the internal feed lives on the interactions Redis; with none
    # configured the honest answer is the empty collection — no store touched.
    if not interactions_store_configured():
        return {"notifications": []}
    _user_id, restricted = request_identity()
    return {"notifications": await read_notifications(audience=restricted)}


@operation(
    summary="Send a human a one-way notification",
    tags=["notifications"],
    destructive=True,
    errors=[BadRequestError, ForbiddenError, NotSupportedError, UnavailableError, UpstreamError],
    request_model=NotifyUser,
)
async def notify_user(
    message: str,
    channel: str | None = None,
    recipient: str | None = None,
    audience: str | None = None,
    sender_identity: str | None = None,
    media: list[MediaItem] | None = None,
    template: ChannelTemplate | None = None,
    options: list[Option] | None = None,
    schema: dict[str, Any] | None = None,
    # Appended after the earlier rich fields so their positional slots are unchanged —
    # the release API gate treats a moved positional parameter as breaking.
    location: LocationElement | None = None,
    sections: list[OptionSection] | None = None,
    header: MediaItem | None = None,
    footer: str | None = None,
) -> str:
    """Send a human a one-way notification, fire-and-forget.

    No reply is expected and nothing blocks. With a named channel the message is
    sent and the call returns as soon as the medium ACCEPTED it (not that a human
    saw it). With ``channel`` omitted the message is recorded to the internal
    notifications sink the Studio inbox reads. One send attempt, no retry; every
    failure raises loudly, never a silent no-op:

    * a blank message with no media to carry it (the contract admits a blank message
      only for a media-only send), an unknown channel name, a blank recipient/audience, a
      caller-supplied ``sender_identity``, a contract-invalid ``media``/``template``/
      ``options``/``schema`` combination (an empty list/dict, an over-cap value, or a
      mutually exclusive
      media+template / options+template / schema+template / schema+options), a ``schema``
      with no channel (the internal sink has no submission door) or one the channel's form
      subset/hook refuses, or a channel's permanent refusal of the input's
      shape (retrying cannot succeed) → 400;
    * a restricted caller addressing another identity (cross-identity denial) → 403;
    * a channel that cannot notify, or does not advertise the
      ``media``/``template``/``options``/``schema`` capability the send needs → 501;
    * a transient channel delivery failure (the raised error's ``retryable``) → 503,
      carrying the medium's ``retry_after`` when it named one;
    * a permanent channel delivery refusal → 502.

    ``media`` (display media sent with the message), ``location`` (a shared map pin),
    ``template`` (a pre-approved out-of-window send), ``options`` (FLAT tappable options a tap
    of which enters the conversation), ``sections`` (the SECTIONED tappable-options
    alternative), ``header``/``footer`` (a media header / trailing line composing an interactive
    message) and ``schema`` (an ask-less form's answer schema — the channel renders the
    message as the form's prompt, and a submission enters the conversation as a message from
    the person) are OPTIONAL richer-send forms — FULL parity with the flow answer path's
    ``AnswerPart`` vocabulary. The contract's shared composition matrix is enforced on this
    request model identically: ``options`` XOR ``sections`` (one choice surface); ``schema``
    excludes both; ``header``/``footer`` require a choice surface; ``template`` is the standalone
    out-of-window send exclusive with every other content/interactive field; ``options``/
    ``sections`` may each combine with ``media``/``location`` (a contract-invalid combination is
    a 400). On a
    named channel each needs a channel that advertises the matching capability
    (``media``→media, ``location``→location, ``template``→template, ``options``/``sections``→
    interactive, ``schema``→form; ``header``/``footer`` ride the choice surface's capability, no
    flag of their own) — otherwise
    the send is refused as a 501, never downgraded to a silent freeform send. With no
    channel media/location/template/options/sections/header/footer are stored on the internal
    inbox record and rendered
    there; ``schema`` alone REQUIRES a channel (a 400 without one) — a stored form nobody
    could submit would be a dead surface, not a notification.

    ``audience`` addresses the in-app record to an identity's feed; it is honored
    even when a channel also delivers the message (channel push AND in-app record).
    ``sender_identity`` is the sending identity a channel message leaves FROM, which the
    conversation bridge sets on the notification it builds to answer a route; a caller may
    not supply it here and a set value is rejected loudly, never forwarded to the channel.

    Returns a short confirmation string — ``"notification sent via '<channel>'"``
    for a channel send, ``"notification recorded to the internal sink"`` otherwise.
    """
    try:
        if sender_identity is not None:
            # Rejected BEFORE any send: a caller must not choose which operator identity a
            # message leaves from.
            raise SenderIdentityNotAllowedError(
                "sender_identity is set internally by the conversation bridge and cannot be supplied by a caller"
            )
        await _notify_user(
            message,
            channel=channel,
            recipient=recipient,
            audience=audience,
            media=media,
            template=template,
            options=options,
            location=location,
            sections=sections,
            header=header,
            footer=footer,
            schema=schema,
        )
    except SenderIdentityNotAllowedError as exc:
        raise BadRequestError(str(exc)) from exc
    except CrossIdentityAudienceError as exc:
        # A restricted caller addressing another identity is a cross-identity boundary
        # violation — an AUTHORIZATION denial (403), the write-side mirror of the read
        # door's ForbiddenError, NOT a bad request. Genuine input-validation errors stay
        # the ValueError→400 below.
        raise ForbiddenError(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except ChannelInputError as exc:
        # A permanent refusal of the input's shape/content — retrying cannot succeed, so it
        # is a client error (400), never a retryable 502 like a delivery failure.
        raise BadRequestError(str(exc)) from exc
    except NotImplementedError as exc:
        raise NotSupportedError(str(exc)) from exc
    except ChannelDeliveryError as exc:
        if exc.retryable:
            # A transient delivery failure (a medium 5xx, a rate limit, a transport fault) may
            # land on a retry: a 503, distinct from the permanent 502 below, carrying the
            # medium's own retry_after when it named one so the caller can honor the wait.
            extra: dict[str, object] | None = {"retry_after": exc.retry_after} if exc.retry_after is not None else None
            raise UnavailableError(str(exc), extra=extra) from exc
        # A permanent delivery refusal (a rejected recipient, a bad credential) cannot be
        # retried — a 502, the non-retryable upstream surface.
        raise UpstreamError(str(exc)) from exc
    if channel is None:
        return "notification recorded to the internal sink"
    return f"notification sent via '{channel}'"
