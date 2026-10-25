"""The Twilio channel: ``deliver`` sends one question, ``notify`` sends one fire-and-forget message.

Tier-1 formats (``confirm``, ``external``) carry the callback_url as a tappable
link and store NO correlation — the human answers via the callback door; confirm
MUST take this path (the door accepts only the link-tap bool). Tier-2 (``text``,
``select``) expect a typed SMS reply matched through the correlation store; the
reservation is written BEFORE the send so a reply racing the send response is
still matchable, and a failed send releases the reservation and raises.

WhatsApp (``whatsapp:``-prefixed pair): a freeform ``Body`` is accepted only
inside Twilio's 24-hour customer-service window, else Twilio rejects with error
63016 as a ``ChannelDeliveryError``. ``reply_window`` reports that window per pair
(from each inbound's receive time); an SMS pair has none.

Media (:class:`MediaItem`) rides ``deliver`` and ``notify`` as MMS/WhatsApp media,
degraded honestly per kind to the text-first medium:

* ``image``/``video``/``audio`` — a renderable media bubble Twilio's MMS envelope
  carries: each attaches as a Twilio ``MediaUrl`` (a public https url Twilio fetches;
  the carrier applies its own per-type fallback). An inline ``data:`` image is
  unrenderable BY NATURE — Twilio fetches a url, not inline bytes — and raises
  :class:`~tai42_contract.channels.ChannelInputError` before any send.
* ``document`` — appended to the ``Body`` as a ``filename: url`` line (its
  suggested download name, which a ``MediaUrl`` cannot carry, followed by the link).
  MMS document rendering is carrier-unreliable, so a named tappable link is the
  honest, readable degradation over an attachment whose name would be lost.
* ``link`` — appended to the ``Body`` as a ``caption: url`` line (SMS carries no rich
  link unfurling; the human taps through).

HONEST CEILING — interactive / location / template / form: an SMS/MMS message has no
tappable buttons, no native map pin, no pre-approved template component model, and no
fillable form. So this channel advertises ONLY ``supports_media_notifications`` and
declares NONE of ``supports_interactive_notifications`` /
``supports_location_notifications`` / ``supports_template_notifications`` /
``supports_form_notifications`` / ``supports_form_delivery``. The platform's central
capability guard (``notify_user`` and the conversations delivery executor) therefore
REFUSES a notification carrying options, sections, a location, a template, or a form
before it ever reaches this channel — a header/footer rides an interactive surface and
is refused transitively — rather than letting the channel fake an affordance the medium
lacks or silently drop the content. The one new-vocabulary surface that reaches this
channel is ``media`` (above). A ``select`` ASK still renders its options as numbered
``Body`` lines and the human answers by TYPING one option (the inbound webhook forwards
the typed reply verbatim to the callback door, which validates it against the option
set) — the medium's honest ceiling, a real text-reply mapping, never a fake button
affordance. Authored option ids never round-trip over SMS: an inbound text matches by
its TEXT, so any author-set id is dropped rather than echoed back.

The recipient allowlist governs ask deliveries; a bridge reply carries a
``sender_identity`` and goes solely to the address that initiated the
conversation, bypassing the allowlist.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from redis.exceptions import RedisError
from tai42_contract.channels import (
    ChannelAddress,
    ChannelAddressError,
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelWithdrawal,
    ReplyWindow,
    SendOutcome,
)
from tai42_contract.interactions import CALLBACK_LINK_FORMATS
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_twilio.client import send_message
from tai42_channel_twilio.correlation import (
    WHATSAPP_ADDRESS_PREFIX,
    release_pending,
    release_pending_for,
    reserve_pending,
)
from tai42_channel_twilio.correlation import reply_window as pair_reply_window
from tai42_channel_twilio.settings import TwilioSettings, require_delivery_setting, twilio_settings

logger = logging.getLogger(__name__)

# The media kinds that ride as an MMS ``MediaUrl`` attachment — a renderable media bubble
# Twilio fetches and delivers. A ``document`` is deliberately NOT here (its suggested
# filename cannot ride a MediaUrl, so it degrades to a named Body line) and neither is a
# ``link`` (a labelled anchor, never an attachment).
_MMS_MEDIA_KINDS = frozenset({MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.AUDIO})

# The default line carrying the tappable callback link on a Tier-1 ask; the caller's
# ``wording.lines.answer_link`` replaces it. The ``{url}`` token is filled with the
# callback url by a plain substitution.
_ANSWER_LINK_LINE = "Answer here: {url}"

# The default hint under the numbered options of a Tier-2 ask that lists choices (a
# ``select`` answer set, or a ``text`` ask's suggested replies); the caller's
# ``wording.choice.reply_hint`` replaces it.
_CHOICE_REPLY_HINT = "Reply with the text of one option."


def _text_media_lines(media: list[MediaItem] | None) -> list[str]:
    """The ``link`` and ``document`` media items rendered as labelled ``Body`` lines.

    SMS/MMS carries no rich link card, and a Twilio ``MediaUrl`` cannot carry a document's
    suggested download name — so a ``link`` rides as ``caption: url`` (the anchor the human
    taps) and a ``document`` as ``filename: url`` (its download name preserved as a tappable
    link; MMS document rendering is carrier-unreliable, so a named link is the honest
    degradation). ``image``/``video``/``audio`` ride as MMS ``MediaUrl`` instead (see
    :func:`_media_urls`).
    """
    lines: list[str] = []
    for item in media or []:
        if item.kind is MediaKind.LINK:
            lines.append(f"{item.caption}: {item.url}" if item.caption else item.url)
        elif item.kind is MediaKind.DOCUMENT:
            label = item.filename or item.caption
            lines.append(f"{label}: {item.url}" if label else item.url)
    return lines


def _media_urls(media: list[MediaItem] | None) -> list[str]:
    """The ``image``/``video``/``audio`` items' urls to attach as MMS ``MediaUrl``.

    The binary media Twilio fetches from a public url and delivers as an MMS
    bubble (the carrier applies its own per-type fallback). Refuses an inline
    ``data:`` image (only ``image`` may carry one; Twilio fetches a public url, an
    inline data URI has none) before any send. ``link``/``document`` ride as
    ``Body`` text lines instead (see :func:`_text_media_lines`).
    """
    urls: list[str] = []
    for item in media or []:
        if item.kind in _MMS_MEDIA_KINDS:
            if item.url.startswith("data:"):
                raise ChannelInputError(
                    "twilio cannot attach an inline data: image; MMS MediaUrl requires a public https url "
                    f"(caption={item.caption!r})"
                )
            urls.append(item.url)
    return urls


def _render_question(delivery: ChannelDelivery) -> str:
    """The SMS body for a Tier-2 ask (``text`` or ``select``).

    Carries the question, any ``link``/``document`` media as labelled lines, plus
    numbered options (a select answer set, or a text ask's suggested replies) the
    human answers by typing one. The hint under the numbered options is the caller's
    ``wording.choice.reply_hint`` when set, else :data:`_CHOICE_REPLY_HINT`.
    """
    lines = [delivery.question, *_text_media_lines(delivery.media)]
    if delivery.options:
        lines.extend(f"{index}. {option}" for index, option in enumerate(delivery.options, start=1))
        choice = delivery.wording.choice if delivery.wording is not None else None
        reply_hint = choice.reply_hint if choice is not None else None
        lines.append(reply_hint or _CHOICE_REPLY_HINT)
    return "\n".join(lines)


def _render_link(delivery: ChannelDelivery) -> str:
    """The SMS body for a Tier-1 ask (``confirm`` or ``external``).

    Carries the question, any ``link``/``document`` media as labelled lines, then
    the tappable callback link. The link line is the caller's
    ``wording.lines.answer_link`` when set, else :data:`_ANSWER_LINK_LINE`, with the
    ``{url}`` token filled by a plain substitution.
    """
    link_wording = delivery.wording.lines if delivery.wording is not None else None
    answer_link = link_wording.answer_link if link_wording is not None else None
    link_line = (answer_link or _ANSWER_LINK_LINE).replace("{url}", delivery.callback_url)
    lines = [delivery.question, *_text_media_lines(delivery.media), "", link_line]
    return "\n".join(lines)


def _target_number(settings: TwilioSettings, requested: str | None) -> str:
    """The "To" number an address names: ``requested`` when set, else ``CHANNEL_TWILIO_DEFAULT_RECIPIENT``.

    The address resolution alone, with no send fence — shared by every outbound send and by a
    withdrawal, so a withdrawal releases the very pair its delivery reserved. No requested number
    and no default is a :class:`ChannelAddressError`.
    """
    if requested is not None:
        return requested
    if not settings.default_recipient:
        raise ChannelAddressError("Twilio channel is not configured: set CHANNEL_TWILIO_DEFAULT_RECIPIENT.")
    return settings.default_recipient


def _resolve_target(settings: TwilioSettings, requested: str | None) -> str:
    """The destination "To" number for an outbound send.

    A caller-requested recipient must be on ``CHANNEL_TWILIO_ALLOWED_RECIPIENTS``
    or is refused loudly with a :class:`ChannelAddressError` (fail closed); no requested
    recipient uses the operator-set ``CHANNEL_TWILIO_DEFAULT_RECIPIENT`` (no allowlist check).
    """
    if requested is not None and requested not in set(settings.allowed_recipients):
        raise ChannelAddressError(
            f"recipient {requested!r} is not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS; refusing to send"
        )
    return _target_number(settings, requested)


class TwilioChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol.

    Advertises ``supports_media_notifications`` (real MMS/WhatsApp media: ``image``/
    ``video``/``audio`` as ``MediaUrl`` attachments, ``document``/``link`` as labelled
    ``Body`` lines). It advertises NO other capability flag: an SMS/MMS message has no
    tappable buttons, native map pin, template component model, or fillable form, so
    ``supports_interactive_notifications`` / ``supports_location_notifications`` /
    ``supports_template_notifications`` / ``supports_form_notifications`` /
    ``supports_form_delivery`` are all absent. The platform's capability guard then refuses
    a notification carrying options, sections, a location, a template, or a form (and a
    header/footer transitively) BEFORE it reaches this channel — no fake affordance, no
    silent drop. A ``select`` ask's options still render as numbered ``Body`` lines answered
    by a typed reply (the honest medium ceiling).
    """

    supports_media_notifications: ClassVar[bool] = True
    # SMS and WhatsApp-over-Twilio carry no typing indicator, so this channel
    # emits no working-on-it signal.
    working_signal_expiry_seconds: ClassVar[None] = None
    # The SMS segment ceiling this channel splits a long answer against; the
    # conversation-delivery machine reads it (an operator may override it per deployment).
    max_message_chars: ClassVar[int] = 1600
    # The address a conversation route on this channel names as its ``our_identity``.
    identity_hint: ClassVar[str] = (
        "The Twilio number clients text, as +15550001111; for a WhatsApp sender write it as whatsapp:+15550001111."
    )

    def resolve_address(self, recipient: str | None, sender_identity: str | None) -> ChannelAddress:
        """The "From" and "To" numbers a send with these values goes from and to.

        ``sender_identity`` set → that number, to ``recipient`` as given (the recipient
        allowlist not consulted: a bridge reply goes to the address that initiated the
        conversation) or ``CHANNEL_TWILIO_DEFAULT_RECIPIENT``; unset →
        ``CHANNEL_TWILIO_FROM``, with ``_resolve_target``'s allowlist enforced. The numbers
        are the ones the inbound webhook passes as ``To`` and ``From``, verbatim (a
        ``whatsapp:`` prefix included). An unresolvable recipient raises
        :class:`ChannelAddressError`; an unset ``CHANNEL_TWILIO_FROM`` raises
        :class:`ChannelDeliveryError`.
        """
        settings = twilio_settings()
        if sender_identity is not None:
            return ChannelAddress(our_identity=sender_identity, address=_target_number(settings, recipient))
        from_number = require_delivery_setting(settings.from_number, "CHANNEL_TWILIO_FROM")
        return ChannelAddress(our_identity=from_number, address=_resolve_target(settings, recipient))

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:
        """Resolve the "From" and "To" numbers, then push the question; returns its outcome.

        Recipient policy is ``resolve_address``'s (fail closed on an unlisted
        request). An ask already past its deadline is refused loudly for every
        format before any reservation or send. The outcome carries the question's
        ``MessageSid``.
        """
        pair = self.resolve_address(delivery.recipient, None)
        from_number, target = pair.our_identity, pair.address

        # Resolve MMS media up front so a data: image is refused before any reservation
        # or send (a permanent input error, never a retryable delivery failure).
        media_urls = _media_urls(delivery.media)

        if delivery.answer_format in CALLBACK_LINK_FORMATS:
            # Tier-1: answered via the callback link, so no correlation is stored.
            sid = await send_message(
                to=target, from_number=from_number, body=_render_link(delivery), media_urls=media_urls or None
            )
            return SendOutcome(our_identity=from_number, address=target, message_ids=[sid])

        # Reserve before send: a fast reply's webhook can beat the send response,
        # and this enforces one-pending-per-pair before any network cost.
        await reserve_pending(
            twilio_number=from_number,
            human_number=target,
            callback_url=delivery.callback_url,
            interaction_id=delivery.interaction_id,
            timeout_at=delivery.timeout_at,
            on_mismatch=delivery.on_mismatch,
            wording=delivery.wording,
        )
        try:
            sid = await send_message(
                to=target, from_number=from_number, body=_render_question(delivery), media_urls=media_urls or None
            )
        except Exception:
            # Send failed — free the pair instead of holding it until its TTL.
            await release_pending(twilio_number=from_number, human_number=target)
            raise
        return SendOutcome(our_identity=from_number, address=target, message_ids=[sid])

    async def withdraw(self, withdrawal: ChannelWithdrawal) -> None:
        """Release what this channel kept for a delivered question the platform has taken down.

        The pair's pending reservation is dropped only while it is still held for
        ``withdrawal.interaction_id`` (a newer ask's hold on the same pair stays). The pair is
        resolved exactly as ``deliver`` resolved it — ``CHANNEL_TWILIO_FROM`` and the recipient, or
        ``CHANNEL_TWILIO_DEFAULT_RECIPIENT`` when none was named — without the allowlist fence,
        which guards a send: a withdrawal sends nothing, and an address removed from the allowlist
        after its delivery is still released. Idempotent, and a no-op when nothing is held (a
        link-answered format never reserved). The question already sent is left as it is. A store
        fault raises a retryable :class:`ChannelDeliveryError`.
        """
        settings = twilio_settings()
        from_number = require_delivery_setting(settings.from_number, "CHANNEL_TWILIO_FROM")
        target = _target_number(settings, withdrawal.recipient)
        try:
            released = await release_pending_for(from_number, target, withdrawal.interaction_id)
        except RedisError as exc:
            raise ChannelDeliveryError(
                f"could not release the withdrawn question {withdrawal.interaction_id}: {exc}", retryable=True
            ) from exc
        logger.info(
            "withdrawn question %s: %s",
            withdrawal.interaction_id,
            "released the pair's reservation" if released else "no reservation held for it",
        )

    async def reply_window(self, *, recipient: str, sender_identity: str | None = None) -> ReplyWindow:
        """WhatsApp's 24-hour customer service window for ``recipient``; ``none`` for an SMS number.

        A ``whatsapp:`` recipient reads the pair with ``sender_identity`` as the Twilio number,
        else the configured ``CHANNEL_TWILIO_FROM`` (unset raises :class:`ChannelDeliveryError`, as
        ``deliver`` does): ``open`` carries the close time (the latest inbound's first receipt + 24
        hours, in whole seconds), ``closed`` means no inbound in the last 24 hours. Any other recipient is an SMS
        number, which has no window. Twilio's 63016 rejection of a send outside the window stays
        final.
        """
        if not recipient.startswith(WHATSAPP_ADDRESS_PREFIX):
            return ReplyWindow(state="none", closes_at=None)
        if sender_identity is not None:
            from_number = sender_identity
        else:
            from_number = require_delivery_setting(twilio_settings().from_number, "CHANNEL_TWILIO_FROM")
        return await pair_reply_window(from_number, recipient)

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any failure.

        Returns the outcome: the "From" and "To" numbers and the single ``MessageSid``
        Twilio assigned the send. No reply is expected, so nothing touches the correlation
        store. Exactly one send attempt (a plain return means Twilio ACCEPTED it, not that a
        human saw it). The numbers are ``resolve_address``'s for the notification's
        recipient and ``sender_identity``. WhatsApp freeform sends obey the 24-hour window
        (error 63016 → ``ChannelDeliveryError``).
        """
        pair = self.resolve_address(notification.recipient, notification.sender_identity)
        from_number, target = pair.our_identity, pair.address
        # Resolve MMS media up front (a data: image is refused here); link/document media
        # rides as appended Body lines.
        media_urls = _media_urls(notification.media)
        text_lines = _text_media_lines(notification.media)
        if notification.message.strip():
            body = "\n".join([notification.message, *text_lines]) if text_lines else notification.message
        else:
            # Media-only (blank message): the Body is just any link/document lines — empty for
            # an images-only send, which goes out as a body-less MMS carrying only its MediaUrl.
            body = "\n".join(text_lines)
        sid = await send_message(to=target, from_number=from_number, body=body, media_urls=media_urls or None)
        return SendOutcome(our_identity=from_number, address=target, message_ids=[sid])
