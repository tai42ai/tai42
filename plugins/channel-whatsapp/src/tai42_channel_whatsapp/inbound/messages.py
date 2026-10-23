"""Per-message-type router: dispatch one inbound message to its handler.

The type→handler dispatch table keeps ``_handle_message`` a flat pipeline
(validate id → mark known contact and advance the reply window → mark read →
extract the message context → dispatch), with the media types sharing one handler via a
bound ``message_type``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.conversations import InboundRejectionReason

from tai42_channel_whatsapp.client import mark_read
from tai42_channel_whatsapp.correlation import already_seen, mark_known_contact, mark_reply_window, mark_seen
from tai42_channel_whatsapp.inbound.params import _message_context, _MessageContext
from tai42_channel_whatsapp.inbound.replies import _handle_button, _handle_interactive, _handle_text
from tai42_channel_whatsapp.inbound.rich_content import (
    _handle_contacts,
    _handle_location,
    _handle_media,
    _handle_reaction,
)

logger = logging.getLogger(__name__)

# Inbound media message types whose object lives under ``message[type]`` (image/document/
# audio/video/sticker). Each fetches the bytes, ingests them through the served-media chokepoint,
# and bridges a turn carrying the typed ``attachments`` entry + parity ``media_*`` params; see the
# INBOUND MEDIA note in ``rich_content``.
_MEDIA_TYPES = frozenset({"image", "document", "audio", "video", "sticker"})

# Vendor message types that are NOT participant content and stay operator-log-only (no reply):
# a system notification and a welcome-request notification. Every other type outside ``_HANDLERS``
# is treated as participant content the channel cannot bridge and receives the generic
# unsupported-type reply.
_NON_CONTENT_NOTICE_TYPES = frozenset({"system", "request_welcome"})

# Meta's inbound error-notice code for a content type it will not deliver ("Unsupported message
# type") — the one error notice that names participant content the channel cannot receive.
_UNSUPPORTED_MESSAGE_TYPE_CODE = 131051

_MessageHandler = Callable[[dict[str, Any], str, str, str, _MessageContext], Awaitable[None]]

# message type → handler with the uniform ``(message, phone_number_id, wa_id, wamid, context)``
# signature. Each media type binds its ``message_type`` onto the shared media handler.
_HANDLERS: dict[str, _MessageHandler] = {
    "text": _handle_text,
    "interactive": _handle_interactive,
    "button": _handle_button,
    "location": _handle_location,
    "contacts": _handle_contacts,
    "reaction": _handle_reaction,
    **{media_type: functools.partial(_handle_media, message_type=media_type) for media_type in _MEDIA_TYPES},
}


def _notice_names_unsupported_type(errors: Any) -> tuple[bool, str | None]:
    """Whether a vendor ``errors`` notice names the unsupported-message-type code, and its title.

    Meta's inbound error-notice shape is ``errors: [{"code": int, "title": str}]``; a
    ``code == 131051`` entry means the participant sent a content type Meta will not deliver. The
    entry's ``title`` is the generic, domain-neutral ``kind`` the rejection event carries.
    """
    if not isinstance(errors, list):
        return False, None
    for entry in errors:
        if isinstance(entry, dict) and entry.get("code") == _UNSUPPORTED_MESSAGE_TYPE_CODE:
            title = entry.get("title")
            return True, title.strip() if isinstance(title, str) and title.strip() else None
    return False, None


async def _notify_unsupported(phone_number_id: str, wa_id: str, wamid: str, *, kind: str) -> None:
    """Send one generic unsupported-type reply and record the platform rejection event.

    Replies only from a known receiving ``phone_number_id`` (the identity to reply from) to a
    present participant ``wa_id``; with neither there is no reply to send and the branch stays
    operator-log-only. Deduped per ``wamid`` so a Meta redelivery never sends a second reply.
    """
    if not phone_number_id or not wa_id:
        return
    if await already_seen(wamid):
        return
    await tai42_app.conversations.notify_inbound_rejected(
        channel_id="whatsapp",
        recipient=wa_id,
        sender_identity=phone_number_id,
        kind=kind,
        reason=InboundRejectionReason.UNSUPPORTED_TYPE,
    )
    await mark_seen(wamid)


def _message_sent_at(message: dict[str, Any], wamid: str) -> datetime | None:
    """The message's vendor ``timestamp`` (unix seconds, a digit string) as an aware UTC datetime.

    A missing or unreadable value logs a WARNING naming the message and the field and gives
    ``None``: the reply window is then not advanced, so it can only read as closed early, never
    open too long.
    """
    raw = message.get("timestamp")
    sent_at: datetime | None = None
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        try:
            sent_at = datetime.fromtimestamp(int(raw), UTC)
        except (OverflowError, OSError, ValueError):
            sent_at = None  # out of the platform's datetime range: unreadable, warned below
    if sent_at is None:
        logger.warning(
            "whatsapp message %s has no readable timestamp field (%r); its reply window is not advanced", wamid, raw
        )
    return sent_at


async def _advance_reply_window(
    message: dict[str, Any], message_type: Any, phone_number_id: str, wa_id: str, wamid: str
) -> None:
    """Advance the pair's reply window from a participant message's own timestamp.

    A platform notice (``_NON_CONTENT_NOTICE_TYPES``) is not the participant writing and leaves
    the window as it is; a message without a readable timestamp is warned about and leaves it too.
    """
    if message_type in _NON_CONTENT_NOTICE_TYPES:
        return
    sent_at = _message_sent_at(message, wamid)
    if sent_at is not None:
        await mark_reply_window(phone_number_id, wa_id, sent_at)


async def _handle_message(message: dict[str, Any], value: dict[str, Any]) -> None:
    """Resolve one inbound message's pending question, or route it to the bridge.

    A message lacking a string id is odd and skipped (logged).
    """
    wamid = message.get("id")
    if not isinstance(wamid, str) or not wamid:
        logger.warning("whatsapp message missing a string id; skipping: %r", message)
        return

    metadata = value.get("metadata")
    phone_number_id = metadata.get("phone_number_id", "") if isinstance(metadata, dict) else ""
    wa_id = message.get("from", "")

    message_type: Any = message.get("type")

    # Record the known-contact marker and advance the reply window for EVERY authenticated
    # inbound BEFORE the message-type drop: a participant who sent only a photo still opened
    # Meta's window.
    if phone_number_id and wa_id:
        await mark_known_contact(phone_number_id, wa_id)
        await _advance_reply_window(message, message_type, phone_number_id, wa_id, wamid)

    # Mark the inbound read the moment it lands — the read receipt for every inbound,
    # sent BEFORE the type branches so it covers text, interactive, media, and
    # correlated question-replies alike. The turn-scoped working signal (the typing
    # indicator) is owned by the skeleton's refresh loop, not this receipt. A delivery
    # failure is logged, never raised: an error here would 5xx the batch and make Meta
    # redeliver it.
    if phone_number_id:
        try:
            await mark_read(phone_number_id, wamid)
        except ChannelDeliveryError as exc:
            logger.warning("whatsapp read receipt for %s failed: %s", wamid, exc)

    # Referral (ctwa/QR entry) and the quoted message are message-level and ride on
    # WHATEVER turn or answer this message becomes, regardless of its type. Extract once, thread down.
    context = _message_context(message)

    handler = _HANDLERS.get(message_type)
    if handler is not None:
        await handler(message, phone_number_id, wa_id, wamid, context)
    elif message.get("errors"):
        # A Meta inbound error notice: always surfaced loudly for the operator, never bridged.
        # An unsupported-message-type notice (``code == 131051``) names participant content the
        # channel cannot receive → reply once with the generic notice + rejection event; every
        # other error code is an operator-facing vendor signal, not participant content, so it
        # stays log-only.
        errors = message.get("errors")
        logger.warning("whatsapp inbound error notice for %s: %r", wamid, errors)
        unsupported, title = _notice_names_unsupported_type(errors)
        if unsupported:
            await _notify_unsupported(phone_number_id, wa_id, wamid, kind=title or str(message_type))
    else:
        # Any other/future type is not bridged; name the type so an operator sees WHAT was
        # dropped, not just that something was. A type outside ``_NON_CONTENT_NOTICE_TYPES`` is
        # participant content the channel cannot bridge → reply once with the generic notice +
        # rejection event; a non-content notification (system / welcome-request) stays log-only.
        logger.info("unhandled whatsapp message type %r for %s; not bridged", message_type, wamid)
        if message_type not in _NON_CONTENT_NOTICE_TYPES:
            await _notify_unsupported(phone_number_id, wa_id, wamid, kind=str(message_type))
