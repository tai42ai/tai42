"""The recorded send of a participant notice over a channel.

Every inbound-side notice a participant receives (a rejected answer, a recognised inbound
content that cannot become a turn) goes out through :func:`send_participant_notice`: a
``notice`` send record of the participant's conversation, bounded by
``delivery_send_timeout_seconds``.
"""

from __future__ import annotations

from tai42_contract.channels import Channel, ChannelNotification, SendOutcome

from tai42_skeleton.channels.send_span import send_span
from tai42_skeleton.conversations.sends import notification_part, recorded_send
from tai42_skeleton.conversations.settings import conversations_settings

__all__ = ["send_participant_notice"]


async def send_participant_notice(channel: Channel, channel_id: str, notification: ChannelNotification) -> None:
    """Send ``notification`` as a recorded ``notice`` send, raising what the send or its record raised."""

    async def send() -> SendOutcome:
        # Tier 1 send-outcome span, gated: in the inbound-webhook context there is no ambient
        # trace, so ``send_span`` no-ops (a rootless span would attach to no run); under an
        # active trace the notify failure is recorded on it.
        with send_span(channel_id, recipient=notification.recipient):
            return await channel.notify(notification)

    await recorded_send(
        channel=channel_id,
        address=channel.resolve_address(notification.recipient, notification.sender_identity),
        parts=[notification_part(notification)],
        sent_by="notice",
        send=send,
        bound=conversations_settings().delivery_send_timeout_seconds,
    )
