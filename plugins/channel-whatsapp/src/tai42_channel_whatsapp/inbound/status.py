"""Delivery-status receipts: a WhatsApp status webhook recorded against its outbound."""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.conversations import DeliveryReceipt

logger = logging.getLogger(__name__)

# WhatsApp delivery status → the terminal receipt to record. Any other status ("sent", a future
# one) is a no-op.
_DELIVERY_RECEIPTS = {
    "failed": DeliveryReceipt.FAILED,
    "delivered": DeliveryReceipt.DELIVERED,
    "read": DeliveryReceipt.DELIVERED,
}


async def handle_status(status: dict[str, Any]) -> None:
    """Record one delivery status against its outbound message.

    ``failed`` → FAILED (loud). ``delivered`` → DELIVERED: Meta delivered the message to the
    WhatsApp user's device. ``read`` → DELIVERED too: a message is read only once delivered, and
    Meta sends no ``delivered`` status for a message read at once. ``sent`` (Meta's servers took
    the message, not the device) is debug-ignored like any other status. A status for a
    ``wamid`` the bridge does not track (``record_delivery_status`` raises ``LookupError``) is
    acked, never a 5xx — the provider must not retry a message we do not own.
    """
    wamid = status.get("id")
    state = status.get("status")
    if not isinstance(wamid, str) or not wamid or not isinstance(state, str) or not state:
        logger.warning("whatsapp status entry missing string id/status; skipping: %r", status)
        return
    receipt = _DELIVERY_RECEIPTS.get(state)
    if receipt is None:
        logger.debug("whatsapp status %r for %s ignored", state, wamid)
        return
    if receipt is DeliveryReceipt.FAILED:
        logger.warning("whatsapp reported delivery failure for %s: %r", wamid, status.get("errors"))
    try:
        await tai42_app.conversations.record_delivery_status("whatsapp", wamid, receipt, errors=status.get("errors"))
    except LookupError as exc:
        # Not a bridge outbound: it may be a ``notify_user`` send with no conversation
        # record. Post the receipt onto the originating trace via the send-outcome index;
        # only a genuine miss (neither the bridge nor such a send owns the id) keeps the
        # untracked-message log.
        if not await tai42_app.channels.record_send_receipt("whatsapp", wamid, receipt, errors=status.get("errors")):
            logger.info("whatsapp status for untracked message %s ignored: %s", wamid, exc)
