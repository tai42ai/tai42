"""The ``app.conversations`` namespace, forwarding to the conversation bridge.

Forwards to the bridge in :mod:`tai42_skeleton.conversations`. ``accept`` turns a received
message into an agent turn and returns the new message's id; ``record_delivery_status`` is the
out-of-band sink an adapter calls when a provider later reports an outbound message's terminal
fate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_contract.conversations import ConversationTargetKind, DeliveryReceipt, TargetBindValidator

if TYPE_CHECKING:
    from tai42_contract.app import PendingMessage
    from tai42_contract.conversations import InboundRejectionReason
    from tai42_contract.interactions.models import LocationElement, MediaItem

    from tai42_skeleton.app.server import TaiMCP


class ConversationsFacet:
    """``app.conversations`` — the bridge's inbound + delivery-receipt entry surface (``AppConversations``)."""

    __slots__ = ("_app",)

    def __init__(self, app: TaiMCP) -> None:
        """Bind the owning ``app``."""
        self._app = app

    async def accept(
        self,
        channel: str,
        our_identity: str,
        client_address: str,
        cap_key: str,
        text: str,
        provider_message_id: str,
        params: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        attachments: list[MediaItem] | None = None,
        location: LocationElement | None = None,
        locale: str | None = None,
        form_tag: str | None = None,
    ) -> str:
        """Turn a received message into an agent turn and return the new message's id."""
        return await self._app._conversation_accept(
            channel,
            our_identity,
            client_address,
            cap_key,
            text,
            provider_message_id,
            params=params,
            form=form,
            attachments=attachments,
            location=location,
            locale=locale,
            form_tag=form_tag,
        )

    async def record_delivery_status(self, channel: str, provider_message_id: str, status: DeliveryReceipt) -> None:
        """Record a provider's terminal delivery status for an outbound message."""
        await self._app._conversation_record_delivery_status(channel, provider_message_id, status)

    async def notify_inbound_rejected(
        self,
        *,
        channel_id: str,
        recipient: str,
        sender_identity: str | None,
        kind: str,
        reason: InboundRejectionReason,
    ) -> None:
        """Reply once and record a platform event when a recognised inbound content cannot become a turn."""
        await self._app._conversation_notify_inbound_rejected(
            channel_id=channel_id,
            recipient=recipient,
            sender_identity=sender_identity,
            kind=kind,
            reason=reason,
        )

    async def pending_messages(self, thread_id: str, *, after: str) -> list[PendingMessage]:
        """The thread's participant messages accepted after ``after`` and not yet carried into a turn."""
        return await self._app._conversation_pending_messages(thread_id, after=after)

    def register_target_validator(
        self, target_kind: ConversationTargetKind, target_name: str, validator: TargetBindValidator
    ) -> None:
        """Register a bind ``validator`` for the ``(target_kind, target_name)`` the plugin owns."""
        self._app._target_validator_registry.register(target_kind, target_name, validator)
