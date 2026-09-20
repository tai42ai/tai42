"""``DeliveryReceipt`` — the terminal fate of an outbound message, as a channel reports it."""

from __future__ import annotations

from enum import StrEnum


class DeliveryReceipt(StrEnum):
    """The terminal fate of an outbound message, as a channel adapter reports it back.

    Reported through ``AppConversations.record_delivery_status``. A channel normalizes its
    provider's vocabulary to these two; intermediate states are not modelled.
    """

    DELIVERED = "delivered"
    FAILED = "failed"
