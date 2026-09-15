"""Per-message-type router: dispatch one inbound message to its handler.

The type→handler dispatch table keeps ``_handle_message`` a flat pipeline
(validate id → mark known contact → typing signal → extract context params →
dispatch), with the media types sharing one handler via a bound ``message_type``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_whatsapp.client import mark_read_typing
from tai42_channel_whatsapp.correlation import mark_known_contact
from tai42_channel_whatsapp.inbound.params import _message_context_params
from tai42_channel_whatsapp.inbound.replies import _handle_button, _handle_interactive, _handle_text
from tai42_channel_whatsapp.inbound.rich_content import (
    _handle_contacts,
    _handle_location,
    _handle_media,
    _handle_reaction,
)

logger = logging.getLogger(__name__)

# Inbound media message types whose object lives under ``message[type]`` (image/document/
# audio/video/sticker). Each bridges as a turn (caption → text, identity → ``media_*`` params);
# see the INBOUND MEDIA design note in ``rich_content`` for why no typed ``attachments`` entry
# is minted.
_MEDIA_TYPES = frozenset({"image", "document", "audio", "video", "sticker"})

_MessageHandler = Callable[[dict[str, Any], str, str, str, dict[str, str]], Awaitable[None]]

# message type → handler with the uniform ``(message, phone_number_id, wa_id, wamid, params)``
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

    # Record the known-contact marker for EVERY authenticated inbound BEFORE the
    # message-type drop: a participant who sent only a photo still opened Meta's window.
    if phone_number_id and wa_id:
        await mark_known_contact(phone_number_id, wa_id)

    # Signal "working on it" the moment an inbound lands — mark it read and show a
    # typing indicator BEFORE the type branches so it covers text, interactive,
    # media, and correlated question-replies alike. A delivery failure is logged,
    # never raised: an error here would 5xx the batch and make Meta redeliver it.
    if phone_number_id:
        try:
            await mark_read_typing(phone_number_id, wamid)
        except ChannelDeliveryError as exc:
            logger.warning("whatsapp typing signal for %s failed: %s", wamid, exc)

    # Referral (ctwa/QR entry) and reply-to context are message-level and ride on
    # WHATEVER turn this message bridges, regardless of its type. Extract once, thread down.
    context_params = _message_context_params(message)

    message_type: Any = message.get("type")
    handler = _HANDLERS.get(message_type)
    if handler is not None:
        await handler(message, phone_number_id, wa_id, wamid, context_params)
    elif message.get("errors"):
        # A Meta inbound error notice (e.g. an unsupported message type the participant sent):
        # never a participant turn — surface it loudly for the operator, do not bridge.
        logger.warning("whatsapp inbound error notice for %s: %r", wamid, message.get("errors"))
    else:
        # Any other/future type is not bridged; name the type so an operator sees WHAT was
        # dropped, not just that something was.
        logger.info("unhandled whatsapp message type %r for %s; not bridged", message_type, wamid)
