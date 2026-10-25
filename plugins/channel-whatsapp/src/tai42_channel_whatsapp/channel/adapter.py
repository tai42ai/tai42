"""The WhatsApp channel protocol adapter: ``deliver`` a question, ``notify`` a message."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic import ValidationError
from redis.exceptions import RedisError
from tai42_contract.channels import (
    ChannelAddress,
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
    ChannelWithdrawal,
    ReplyWindow,
    SendOutcome,
)
from tai42_contract.interactions import CALLBACK_LINK_FORMATS

from tai42_channel_whatsapp.channel.asks import _render_link, _send_question
from tai42_channel_whatsapp.channel.forms import _deliver_form, _send_form_notification
from tai42_channel_whatsapp.channel.interactive import _INTERACTIVE_BODY_MAX_CHARS, _choice_of, _guard_choice_label
from tai42_channel_whatsapp.channel.media import _send_media_prelude
from tai42_channel_whatsapp.channel.notifications import _send_notification
from tai42_channel_whatsapp.channel.recipients import _NO_DEFAULT_RECIPIENT, _require_recipient, _send_template
from tai42_channel_whatsapp.channel.template_params import parse_template_parameters
from tai42_channel_whatsapp.client import SentMessage, mark_read_typing, send_message, send_outcome
from tai42_channel_whatsapp.correlation import (
    drop_endpoint_form,
    release_pending,
    release_pending_for,
    reserve_pending,
)
from tai42_channel_whatsapp.correlation import reply_window as pair_reply_window
from tai42_channel_whatsapp.flows import build_form_flow
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
    # This channel renders a form ask as a WhatsApp Flow; the ask helper reads
    # this before handing a form delivery over.
    supports_form_delivery: ClassVar[bool] = True
    # This channel routes a reacting form's triggers back through its encrypted Flow data
    # endpoint; the ask helper reads this (on top of supports_form_delivery) before admitting a
    # REACTING form — one whose ask names a reaction_tool and carries reactions triggers — so a
    # reacting form is never delivered to a medium that cannot route its reactions.
    supports_form_reaction: ClassVar[bool] = True
    # Meta dismisses the typing indicator after 25 s (or on the reply); the skeleton's
    # refresh loop re-asserts it under this lifetime until the answer is sent.
    # https://developers.facebook.com/docs/whatsapp/cloud-api/guides/mark-messages-as-read#typing-indicators
    working_signal_expiry_seconds: ClassVar[float] = 25.0
    # Meta caps a text message body at 4096 characters; the conversation-delivery machine splits a
    # long answer against this (an operator may override it per deployment).
    max_message_chars: ClassVar[int] = 4096
    # The address a conversation route on this channel names as its ``our_identity``.
    identity_hint: ClassVar[str] = "The number's phone_number_id from the Meta dashboard, not the display number."

    def validate_template(self, template: ChannelTemplate) -> None:
        """Enforce this channel's template-parameter schema, before any send.

        The template's opaque ``parameters`` are parsed into this channel's own
        :class:`WhatsAppTemplateParameters` model (header media argument, body-text parameters,
        per-button runtime arguments) and mapped the same way ``send_template`` maps them; a shape
        this channel could never render to Meta's template components raises ``ValueError``, so an
        unmappable template is refused up front instead of failing at delivery.
        """
        try:
            parse_template_parameters(template.parameters)
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"invalid WhatsApp template parameters: {exc}") from exc

    def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
        """Enforce this channel's form-schema limits at ask-time, before any state is written.

        The reserved ``flow_token`` property (Meta's own correlation
        key) — and every subset rule the Flow mapping enforces — is refused here as
        a ``ValueError``, so a schema the delivery path could never render is
        rejected up front instead of persisting a question that only fails at
        delivery. ``build_form_flow`` is the single mapping definition — the builder
        that actually sends is the one that refuses; a delivery-time
        ``ChannelInputError`` becomes the ask-time ``ValueError``. The Flow body is
        ``interactive.body.text``, capped by Meta at ``_INTERACTIVE_BODY_MAX_CHARS``,
        so an over-long ``question`` is refused here too.
        """
        if len(question) > _INTERACTIVE_BODY_MAX_CHARS:
            raise ValueError(f"form question exceeds {_INTERACTIVE_BODY_MAX_CHARS} characters")
        try:
            build_form_flow(schema)
        except ChannelInputError as exc:
            raise ValueError(str(exc)) from exc

    def resolve_address(self, recipient: str | None, sender_identity: str | None) -> ChannelAddress:
        """The ``phone_number_id`` and ``wa_id`` a send would use, known before sending.

        The identity is ``sender_identity`` when set, else the configured
        ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID`` (missing → :class:`ChannelDeliveryError`).
        The address is ``recipient`` as given: this channel has no default recipient, so none
        raises :class:`ChannelAddressError`. A completed send reports the ``wa_id`` Meta
        resolved it to, which may differ in form from ``recipient``. The template recipient
        fence reads the contact store, so it runs at send time, never here.
        """
        if sender_identity is not None:
            target = _require_recipient(recipient, "a bridge reply requires a recipient wa_id; none was provided")
            return ChannelAddress(our_identity=sender_identity, address=target)
        phone_number_id = require_delivery_setting(
            whatsapp_settings().default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
        )
        target = _require_recipient(recipient, _NO_DEFAULT_RECIPIENT)
        return ChannelAddress(our_identity=phone_number_id, address=target)

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:
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
        so an interactive tap resolves back to the exact option text. Returns the
        :class:`SendOutcome` of every message sent, the media first.
        """
        settings = whatsapp_settings()
        address = self.resolve_address(delivery.recipient, None)
        phone_number_id, target = address.our_identity, address.address

        # Display media rides ahead of the question — sent before any reservation, so
        # a media failure raises with nothing reserved.
        sent: list[SentMessage] = []
        if delivery.media:
            sent = await _send_media_prelude(phone_number_id, target, list(delivery.media))

        if delivery.answer_format in CALLBACK_LINK_FORMATS:
            # Tier-1: answered via the callback link, so no correlation is stored.
            sent.append(await send_message(phone_number_id=phone_number_id, to=target, body=_render_link(delivery)))
            return send_outcome(phone_number_id, sent)

        if delivery.answer_format == "form":
            sent.append(await _deliver_form(settings, phone_number_id, target, delivery))
            return send_outcome(phone_number_id, sent)

        # A select ask's list-opening button carries the caller's ``wording.choice.label``; refuse
        # an over-cap label loudly BEFORE the reservation and any send, regardless of the shape the
        # options end up rendering as (buttons / list / numbered text).
        choice = _choice_of(delivery)
        _guard_choice_label(choice.label if choice is not None else None)

        # Reserve before send: a fast reply's webhook can beat the send response,
        # and this enforces one-pending-per-pair before any network cost. The reservation
        # carries the ask's digression policy and per-send wording so the shared answer ladder
        # honours them and draws the caller's refusal sentence off the stored record.
        await reserve_pending(
            phone_number_id=phone_number_id,
            wa_id=target,
            callback_url=delivery.callback_url,
            timeout_at=delivery.timeout_at,
            options=delivery.options,
            interaction_id=delivery.interaction_id,
            on_mismatch=delivery.on_mismatch,
            wording=delivery.wording,
        )
        try:
            sent.extend(await _send_question(phone_number_id, target, delivery))
        except Exception:
            # Send failed — free the pair instead of holding it until its TTL.
            await release_pending(phone_number_id=phone_number_id, wa_id=target)
            raise
        return send_outcome(phone_number_id, sent)

    async def withdraw(self, withdrawal: ChannelWithdrawal) -> None:
        """Release what this channel kept for a delivered question the platform has taken down.

        The pair's pending reservation is dropped only while it is still held for
        ``withdrawal.interaction_id`` (a newer ask's hold on the same pair stays), and an
        endpoint-driven form's data-endpoint sidecar is deleted. The pair is resolved exactly as ``deliver``
        resolved it: the configured default ``phone_number_id`` and the recipient ``wa_id``; a
        withdrawal with no recipient holds nothing, since ``deliver`` refuses a missing recipient
        before any reservation. Idempotent, and a no-op when nothing is held (a link-answered
        format never reserved). The question already in the chat is left as it is. A store fault
        raises a retryable :class:`ChannelDeliveryError`.
        """
        if withdrawal.recipient is None:
            return
        phone_number_id = require_delivery_setting(
            whatsapp_settings().default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
        )
        try:
            released = await release_pending_for(phone_number_id, withdrawal.recipient, withdrawal.interaction_id)
            await drop_endpoint_form(withdrawal.interaction_id)
        except RedisError as exc:
            raise ChannelDeliveryError(
                f"could not release the withdrawn question {withdrawal.interaction_id}: {exc}", retryable=True
            ) from exc
        logger.info(
            "withdrawn question %s: %s",
            withdrawal.interaction_id,
            "released the pair's reservation" if released else "no reservation held for it",
        )

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any failure.

        Returns the :class:`SendOutcome`: the sending ``phone_number_id``, the ``wa_id`` Meta
        resolved the recipient to, and every ``wamid`` the Cloud API assigned, in send order.

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
        address = self.resolve_address(notification.recipient, notification.sender_identity)
        phone_number_id, target = address.our_identity, address.address

        if notification.template is not None:
            sent = await _send_template(settings, phone_number_id, target, notification.template)
        elif notification.schema is not None:
            sent = await _send_form_notification(settings, phone_number_id, target, notification)
        else:
            sent = await _send_notification(phone_number_id, target, notification)
        return send_outcome(phone_number_id, sent)

    async def reply_window(self, *, recipient: str, sender_identity: str | None = None) -> ReplyWindow:
        """Meta's 24-hour customer service window for ``recipient`` (a ``wa_id``) on one number.

        The number is ``sender_identity`` (a ``phone_number_id``), else the configured
        ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID``; with neither the pair is unaddressable and
        :class:`ChannelDeliveryError` is raised, as ``notify`` raises. ``open`` carries the close
        time (the participant's latest message + 24 hours); ``closed`` means no participant message
        in the last 24 hours. Meta's 131047 rejection of a send outside the window stays final.
        """
        if sender_identity is not None:
            phone_number_id = sender_identity
        else:
            phone_number_id = require_delivery_setting(
                whatsapp_settings().default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
            )
        return await pair_reply_window(phone_number_id, recipient)

    async def signal_working(
        self,
        *,
        recipient: str,
        sender_identity: str | None = None,
        provider_message_id: str | None = None,
        active: bool = True,
    ) -> None:
        """Assert the "working-on-it" typing indicator against the inbound message.

        Meta ties the typing indicator to the mark-as-read request for a specific
        inbound ``wamid`` and offers no explicit stop — it dismisses on the reply or
        after 25 s — so ``active=False`` is a documented no-op. With no inbound
        ``provider_message_id`` there is no message to attach typing to, so the call
        returns quietly. Otherwise it resolves the sending ``phone_number_id`` exactly
        as ``notify`` does (``sender_identity`` else the configured default) and
        re-sends the combined read+typing body (re-marking read is idempotent). A send
        failure raises ``ChannelDeliveryError`` for the caller's refresh loop to log
        and count.
        """
        if not active:
            return
        if provider_message_id is None:
            return
        if sender_identity is not None:
            phone_number_id = sender_identity
        else:
            phone_number_id = require_delivery_setting(
                whatsapp_settings().default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
            )
        await mark_read_typing(phone_number_id, provider_message_id)
