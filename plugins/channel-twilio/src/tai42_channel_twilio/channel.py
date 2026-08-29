"""The Twilio channel: ``deliver`` sends one question, ``notify`` sends one
fire-and-forget message.

Tier-1 formats (``confirm``, ``external``) carry the callback_url as a tappable
link and store NO correlation — the human answers via the callback door; confirm
MUST take this path (the door accepts only the link-tap bool). Tier-2 (``text``,
``select``) expect a typed SMS reply matched through the correlation store; the
reservation is written BEFORE the send so a reply racing the send response is
still matchable, and a failed send releases the reservation and raises.

WhatsApp (``whatsapp:``-prefixed pair): a freeform ``Body`` is accepted only
inside Twilio's 24-hour customer-service window, else Twilio rejects with error
63016 as a ``ChannelDeliveryError``.

Media (:class:`MediaItem`) rides ``deliver`` and ``notify`` as MMS/WhatsApp media:
each ``image`` item attaches as a Twilio ``MediaUrl`` (a public https url — a
``data:`` image is unrenderable BY NATURE and raises
:class:`~tai42_contract.channels.ChannelInputError`), and each ``link`` item is
appended to the ``Body`` as a labelled line (SMS carries no rich link unfurling).

HONEST CEILING — options: an SMS/MMS message has no tappable buttons, so this
channel does NOT advertise ``supports_interactive_notifications`` and never receives
a ``notify`` carrying tappable options (the central guard refuses it up front). A
``select`` ASK still renders its options as numbered ``Body`` lines and the human
answers by TYPING one option (the inbound webhook forwards the typed reply verbatim
to the callback door, which validates it against the option set) — the medium's
honest ceiling, a real text-reply mapping, never a fake button affordance.

The recipient allowlist governs ask_user deliveries; a bridge reply carries a
``sender_identity`` and goes solely to the address that initiated the
conversation, bypassing the allowlist.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import ClassVar

from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_twilio.client import send_message
from tai42_channel_twilio.correlation import release_pending, reserve_pending
from tai42_channel_twilio.settings import TwilioSettings, require_delivery_setting, twilio_settings

# Tier-1 answer formats resolve via the callback link, not an SMS reply.
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _link_lines(media: list[MediaItem] | None) -> list[str]:
    """The ``link`` media items rendered as labelled ``Body`` lines (SMS has no rich
    link cards, so a link rides as text)."""
    return [
        f"{item.caption}: {item.url}" if item.caption else item.url
        for item in (media or [])
        if item.kind is MediaKind.LINK
    ]


def _image_media_urls(media: list[MediaItem] | None) -> list[str]:
    """The ``image`` items' urls to attach as MMS ``MediaUrl`` — refusing a ``data:``
    image (Twilio fetches a public url; an inline data URI has none) before any send."""
    urls: list[str] = []
    for item in media or []:
        if item.kind is MediaKind.IMAGE:
            if item.url.startswith("data:"):
                raise ChannelInputError(
                    "twilio cannot attach an inline data: image; MMS MediaUrl requires a public https url "
                    f"(caption={item.caption!r})"
                )
            urls.append(item.url)
    return urls


def _render_question(delivery: ChannelDelivery) -> str:
    """The SMS body for a Tier-2 ask (``text`` or ``select``): the question, any
    ``link`` media as labelled lines, plus numbered options (a select answer set, or a
    text ask's suggested replies) the human answers by typing one."""
    lines = [delivery.question, *_link_lines(delivery.media)]
    if delivery.options:
        lines.extend(f"{index}. {option}" for index, option in enumerate(delivery.options, start=1))
        lines.append("Reply with the text of one option.")
    return "\n".join(lines)


def _render_link(delivery: ChannelDelivery) -> str:
    """The SMS body for a Tier-1 ask (``confirm`` or ``external``): the question, any
    ``link`` media as labelled lines, then the tappable callback link."""
    lines = [delivery.question, *_link_lines(delivery.media), "", f"Answer here: {delivery.callback_url}"]
    return "\n".join(lines)


def _resolve_target(settings: TwilioSettings, requested: str | None) -> str:
    """The destination "To" number for an outbound send.

    A caller-requested recipient must be on ``CHANNEL_TWILIO_ALLOWED_RECIPIENTS``
    or is refused loudly (fail closed); no requested recipient uses the
    operator-set ``CHANNEL_TWILIO_DEFAULT_RECIPIENT`` (no allowlist check).
    """
    if requested is None:
        return require_delivery_setting(settings.default_recipient, "CHANNEL_TWILIO_DEFAULT_RECIPIENT")
    if requested not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {requested!r} is not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS; refusing to send"
        )
    return requested


class TwilioChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol.

    Advertises ``supports_media_notifications`` (real MMS/WhatsApp media). It does NOT
    advertise ``supports_interactive_notifications``: an SMS/MMS message has no tappable
    buttons, so a ``notify`` with options is refused up front rather than sent with a
    fake affordance. A ``select`` ask's options still render as numbered ``Body`` lines
    answered by a typed reply (the honest medium ceiling).
    """

    supports_media_notifications: ClassVar[bool] = True

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Resolve the "To" number, then push the question to it.

        Recipient policy is ``_resolve_target``'s (fail closed on an unlisted
        request). An ask already past its deadline is refused loudly for every
        format before any reservation or send.
        """
        settings = twilio_settings()
        from_number = require_delivery_setting(settings.from_number, "CHANNEL_TWILIO_FROM")
        target = _resolve_target(settings, delivery.recipient)

        # Refuse a question whose answer budget is already spent, before any
        # reservation or HTTP work.
        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        # Resolve MMS media up front so a data: image is refused before any reservation
        # or send (a permanent input error, never a retryable delivery failure).
        media_urls = _image_media_urls(delivery.media)

        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1: answered via the callback link, so no correlation is stored.
            await send_message(
                to=target, from_number=from_number, body=_render_link(delivery), media_urls=media_urls or None
            )
            return

        # Reserve before send: a fast reply's webhook can beat the send response,
        # and this enforces one-pending-per-pair before any network cost.
        await reserve_pending(
            twilio_number=from_number,
            human_number=target,
            callback_url=delivery.callback_url,
            interaction_id=delivery.interaction_id,
            timeout_at=delivery.timeout_at,
        )
        try:
            await send_message(
                to=target, from_number=from_number, body=_render_question(delivery), media_urls=media_urls or None
            )
        except Exception:
            # Send failed — free the pair instead of holding it until its TTL.
            await release_pending(twilio_number=from_number, human_number=target)
            raise

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any
        failure. Returns the single ``MessageSid`` Twilio assigned the send.

        No reply is expected, so nothing touches the correlation store. Exactly
        one send attempt (a plain return means Twilio ACCEPTED it, not that a
        human saw it). ``sender_identity`` set → send FROM it verbatim to the
        recipient, the recipient allowlist not consulted (a bridge reply goes to
        the address that initiated the conversation); unset → the configured
        ``CHANNEL_TWILIO_FROM`` with ``_resolve_target``'s allowlist enforced.
        WhatsApp freeform sends obey the 24-hour window (error 63016 →
        ``ChannelDeliveryError``).
        """
        settings = twilio_settings()
        if notification.sender_identity is not None:
            from_number = notification.sender_identity
            target = notification.recipient or require_delivery_setting(
                settings.default_recipient, "CHANNEL_TWILIO_DEFAULT_RECIPIENT"
            )
        else:
            from_number = require_delivery_setting(settings.from_number, "CHANNEL_TWILIO_FROM")
            target = _resolve_target(settings, notification.recipient)
        # Resolve MMS media up front (a data: image is refused here); link media rides
        # as appended Body lines.
        media_urls = _image_media_urls(notification.media)
        link_lines = _link_lines(notification.media)
        if notification.message.strip():
            body = "\n".join([notification.message, *link_lines]) if link_lines else notification.message
        else:
            # Media-only (blank message): the Body is just any link lines — empty for an
            # images-only send, which goes out as a body-less MMS carrying only its MediaUrl.
            body = "\n".join(link_lines)
        sid = await send_message(to=target, from_number=from_number, body=body, media_urls=media_urls or None)
        return [sid]
