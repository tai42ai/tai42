"""Notifications operations — read the internal inbox and send a notification.

* ``list_notifications`` returns the deployment's internal notifications feed —
  channel-less sends plus any audience-addressed notification (recorded even when
  a channel also delivers it) — newest-first.
* ``notify_user`` sends a human ONE fire-and-forget message — on a named channel,
  or (channel omitted) recorded to the internal notifications sink the Studio inbox
  reads. No reply, no blocking wait. It delivers over the channels feature's
  ``notify_user`` helper, mapping the helper's loud
  failures to the operation's typed errors: a blank message / unknown channel /
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

from pydantic import BaseModel, Field
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError, ChannelTemplate
from tai42_contract.interactions.models import MediaItem

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
            "Optional display media sent WITH the message on the named channel. Requires a channel that "
            "advertises media support (else a 501) and a named channel at all (else a 400); mutually "
            "exclusive with template."
        ),
    )
    template: ChannelTemplate | None = Field(
        default=None,
        description=(
            "Optional pre-approved template for an out-of-window send on the named channel. Requires a "
            "channel that advertises template support (else a 501) and a named channel at all (else a "
            "400); mutually exclusive with media and options."
        ),
    )
    options: list[str] | None = Field(
        default=None,
        description=(
            "Optional tappable options sent WITH the message on the named channel; a tap enters the "
            "conversation as a visitor message on channels that support it. Requires a channel that "
            "advertises interactive support (else a 501) and a named channel at all (else a 400); "
            "mutually exclusive with template, may combine with media."
        ),
    )


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
    options: list[str] | None = None,
) -> str:
    """Send a human a one-way notification, fire-and-forget.

    No reply is expected and nothing blocks. With a named channel the message is
    sent and the call returns as soon as the medium ACCEPTED it (not that a human
    saw it). With ``channel`` omitted the message is recorded to the internal
    notifications sink the Studio inbox reads. One send attempt, no retry; every
    failure raises loudly, never a silent no-op:

    * a blank message, an unknown channel name, a blank recipient/audience, a
      caller-supplied ``sender_identity``, ``media``/``template``/``options`` with no
      named channel, or a channel's permanent refusal of the input's shape (retrying
      cannot succeed) → 400;
    * a restricted caller addressing another identity (cross-identity denial) → 403;
    * a channel that cannot notify, or does not advertise the
      ``media``/``template``/``options`` capability the send needs → 501;
    * a transient channel delivery failure (the raised error's ``retryable``) → 503,
      carrying the medium's ``retry_after`` when it named one;
    * a permanent channel delivery refusal → 502.

    ``media`` (display media sent with the message), ``template`` (a pre-approved
    out-of-window send) and ``options`` (tappable options a tap of which enters the
    conversation) are OPTIONAL richer-send forms carried to the channel; the contract
    enforces media/template and options/template are each mutually exclusive (options may
    combine with media). Each needs a channel that advertises the matching capability —
    otherwise the send is refused as a 501, never downgraded to a silent freeform send.

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
