"""The one reply for an inbound message the channel recognises but cannot turn into a turn or an answer."""

from __future__ import annotations

import logging

from tai42_contract.app import tai42_app
from tai42_contract.conversations import InboundRejectionReason

from tai42_channel_whatsapp.correlation import already_seen, mark_seen

logger = logging.getLogger(__name__)


async def notify_rejected(
    phone_number_id: str, wa_id: str, wamid: str, *, kind: str, reason: InboundRejectionReason
) -> None:
    """Send one reason-typed rejection notice and record the platform rejection event.

    Replies only from a known receiving ``phone_number_id`` (the identity to reply from) to a
    present participant ``wa_id``; with either missing there is no reply to send, and a WARNING
    names the ``wamid`` and the missing field. Deduped per ``wamid`` so a Meta redelivery never
    sends a second reply.
    """
    missing = "phone_number_id" if not phone_number_id else "from" if not wa_id else None
    if missing is not None:
        logger.warning(
            "whatsapp inbound %s: cannot reply (no %s); %s %s not answered", wamid, missing, reason.value, kind
        )
        return
    if await already_seen(wamid):
        return
    await tai42_app.conversations.notify_inbound_rejected(
        channel_id="whatsapp",
        recipient=wa_id,
        sender_identity=phone_number_id,
        kind=kind,
        reason=reason,
    )
    await mark_seen(wamid)
