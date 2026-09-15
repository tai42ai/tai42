"""The WhatsApp channel protocol adapter: ``deliver`` a question, ``notify`` a message."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any, ClassVar

from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
)

from tai42_channel_whatsapp.channel.asks import _TIER1_FORMATS, _render_link, _send_question
from tai42_channel_whatsapp.channel.forms import _deliver_form, _send_form_notification
from tai42_channel_whatsapp.channel.interactive import _INTERACTIVE_BODY_MAX_CHARS
from tai42_channel_whatsapp.channel.media import _send_media_prelude
from tai42_channel_whatsapp.channel.notifications import _send_notification
from tai42_channel_whatsapp.channel.recipients import _NO_DEFAULT_RECIPIENT, _require_recipient, _send_template
from tai42_channel_whatsapp.client import send_message
from tai42_channel_whatsapp.correlation import release_pending, reserve_pending
from tai42_channel_whatsapp.flows import build_flow
from tai42_channel_whatsapp.settings import require_delivery_setting, whatsapp_settings

logger = logging.getLogger(__name__)


class WhatsAppChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol."""

    # This channel sends media (image/document/video/audio + link) and out-of-window
    # templates, shares a geographic location, renders a notification's tappable
    # options/sections as native reply buttons/list/cta_url (with a media header + footer),
    # and renders an ask-less form notification as a WhatsApp Flow; the central notify_user
    # (and conversation-delivery) capability guard reads these before dispatching each.
    supports_media_notifications: ClassVar[bool] = True
    supports_location_notifications: ClassVar[bool] = True
    supports_template_notifications: ClassVar[bool] = True
    supports_interactive_notifications: ClassVar[bool] = True
    supports_form_notifications: ClassVar[bool] = True
    # This channel renders a form ask as a WhatsApp Flow; the ask_user helper reads
    # this before handing a form delivery over.
    supports_form_delivery: ClassVar[bool] = True

    def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
        """Enforce this channel's form-schema limits at ask-time, before any state is written.

        The reserved ``flow_token`` property (Meta's own correlation
        key) — and every subset rule the Flow mapping enforces — is refused here as
        a ``ValueError``, so a schema the delivery path could never render is
        rejected up front instead of persisting a question that only fails at
        delivery. ``build_flow`` is the single mapping definition; a delivery-time
        ``ChannelInputError`` becomes the ask-time ``ValueError``. The Flow body is
        ``interactive.body.text``, capped by Meta at ``_INTERACTIVE_BODY_MAX_CHARS``,
        so an over-long ``question`` is refused here too.
        """
        if len(question) > _INTERACTIVE_BODY_MAX_CHARS:
            raise ValueError(f"form question exceeds {_INTERACTIVE_BODY_MAX_CHARS} characters")
        try:
            build_flow(schema)
        except ChannelInputError as exc:
            raise ValueError(str(exc)) from exc

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Resolve the destination ``wa_id``, then push the question to it.

        The question is a freeform send, so the recipient is required but not
        allowlist-fenced (Meta's 24-hour window is the fence). An ask already past
        its deadline is refused loudly for every format before any reservation or
        send. Any accompanying display ``media`` is sent FIRST — each ``image`` as
        its own image message and any ``link`` items as a text line-block, the same
        per-item send ``notify`` uses — so the question (carrying any tappable
        widget) stays the last, actionable message; a media failure raises before
        any reservation. A select ask renders in its native shape (buttons/list/
        numbered text); the reservation carries the ask's options and interaction id
        so an interactive tap resolves back to the exact option text.
        """
        settings = whatsapp_settings()
        phone_number_id = require_delivery_setting(
            settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
        )
        target = _require_recipient(delivery.recipient, _NO_DEFAULT_RECIPIENT)

        # Refuse a question whose answer budget is already spent, before any
        # reservation or HTTP work.
        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        # Display media rides ahead of the question — sent before any reservation, so
        # a media failure raises with nothing reserved.
        if delivery.media:
            await _send_media_prelude(phone_number_id, target, list(delivery.media))

        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1: answered via the callback link, so no correlation is stored.
            await send_message(phone_number_id=phone_number_id, to=target, body=_render_link(delivery))
            return

        if delivery.answer_format == "form":
            await _deliver_form(settings, phone_number_id, target, delivery)
            return

        # Reserve before send: a fast reply's webhook can beat the send response,
        # and this enforces one-pending-per-pair before any network cost.
        await reserve_pending(
            phone_number_id=phone_number_id,
            wa_id=target,
            callback_url=delivery.callback_url,
            timeout_at=delivery.timeout_at,
            options=delivery.options,
            interaction_id=delivery.interaction_id,
        )
        try:
            await _send_question(phone_number_id, target, delivery)
        except Exception:
            # Send failed — free the pair instead of holding it until its TTL.
            await release_pending(phone_number_id=phone_number_id, wa_id=target)
            raise

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any failure.

        Returns every ``wamid`` the Cloud API assigned, in send order.

        No reply is expected, so nothing touches the correlation store. Exactly one
        send attempt per part (a plain return means Meta ACCEPTED it, not that a
        human saw it). ``sender_identity`` set → send FROM that ``phone_number_id``;
        unset → the configured ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID``. When
        ``template`` is set the send is the out-of-window template (recipient
        allowlist-or-known-contact); when ``schema`` is set the send is an ask-less
        FORM — any media as the prelude, then a WhatsApp Flow whose body is the
        message and whose flow token rides the ``tai42-nf:`` namespace (a submission
        enters the conversation as a structured visitor message; nothing is
        reserved); otherwise the freeform body plus any tappable ``options`` (native
        reply buttons/list — a tap enters the conversation as a visitor message) and
        any media parts (freeform recipient unfenced). The recipient is always
        required.
        """
        settings = whatsapp_settings()
        if notification.sender_identity is not None:
            phone_number_id = notification.sender_identity
            target = _require_recipient(
                notification.recipient, "a bridge reply requires a recipient wa_id; none was provided"
            )
        else:
            phone_number_id = require_delivery_setting(
                settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
            )
            target = _require_recipient(notification.recipient, _NO_DEFAULT_RECIPIENT)

        if notification.template is not None:
            return await _send_template(settings, phone_number_id, target, notification.template)
        if notification.schema is not None:
            return await _send_form_notification(settings, phone_number_id, target, notification)
        return await _send_notification(phone_number_id, target, notification)
