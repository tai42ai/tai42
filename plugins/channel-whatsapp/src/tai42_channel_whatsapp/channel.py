"""The WhatsApp channel: ``deliver`` sends one question, ``notify`` sends
one fire-and-forget message.

Tier-1 formats (``confirm``, ``external``) carry the callback_url as a tappable
link and store NO correlation — the human answers via the callback door; confirm
MUST take this path (the door accepts only the link-tap bool). Tier-2 (``text``,
``select``) expect a WhatsApp reply matched through the correlation store; the
reservation is written BEFORE the send so a reply racing the send response is
still matchable, and a failed send releases the reservation and raises. A
``select`` ask renders natively where it fits — interactive reply buttons for a
few short options, an interactive list for more, and the numbered-text fallback
past those caps — and the human may always type an option instead of tapping. A
``form`` ask is also Tier-2: it renders as an in-chat WhatsApp Flow (created and
published once per answer schema, then reused), and the completed form returns as
an ``nfm_reply`` matched by the delivery's ``interaction_id`` as the flow token.

Freeform sends (questions, replies, media) go to any requested recipient: Meta's
own 24-hour customer-service window is the fence (a send outside it is rejected,
error 131047, and raises). A TEMPLATE send is the one send Meta delivers cold, so
it keeps an operator fence — the recipient must be on
``CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS`` or be a known contact (a pair the inbound
webhook saw within the configured window). A recipient is always caller-supplied;
this channel has no default recipient.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any, ClassVar

from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelNotification,
    ChannelTemplate,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.client import (
    create_flow,
    delete_flow,
    publish_flow,
    send_flow,
    send_image,
    send_interactive_buttons,
    send_interactive_list,
    send_message,
    send_template,
)
from tai42_channel_whatsapp.correlation import (
    cache_flow_id,
    get_cached_flow_id,
    is_known_contact,
    release_pending,
    reserve_pending,
)
from tai42_channel_whatsapp.flows import build_flow
from tai42_channel_whatsapp.settings import (
    WhatsAppSettings,
    require_delivery_setting,
    whatsapp_settings,
)

logger = logging.getLogger(__name__)

# Tier-1 answer formats resolve via the callback link, not a WhatsApp reply.
_TIER1_FORMATS = frozenset({"confirm", "external"})

# WhatsApp interactive-message caps (Meta Cloud API). A select ask renders as
# reply buttons when its options fit the button caps, else as a list when they
# fit the list caps, else as numbered text. An option longer than the tier's
# title cap forces the fallback for the WHOLE ask — a truncated title would show
# the human a choice that differs from what the answer validates against.
_BUTTON_MAX_COUNT = 3  # reply-buttons: at most three buttons
_BUTTON_TITLE_MAX_CHARS = 20  # reply-button title (also must be unique)
_LIST_MAX_ROWS = 10  # list: at most ten rows across all sections
_LIST_ROW_TITLE_MAX_CHARS = 24  # list-row title
_INTERACTIVE_BODY_MAX_CHARS = 1024  # interactive body text
# The list-opening button label (its own 20-char cap); a fixed, generic prompt.
_LIST_BUTTON_LABEL = "Choose an option"

# A Flow's name on Meta, the schema-hash suffix making it a deterministic label
# for operator legibility — NOT a uniqueness key: Meta does not enforce flow-name
# uniqueness. One Flow per distinct answer schema comes from the cache plus the
# orphan-draft cleanup, never from the name.
_FLOW_NAME_PREFIX = "tai42-form-"


async def _resolve_flow_id(waba_id: str, schema_hash: str, flow_json: dict[str, Any]) -> str:
    """The published flow id for this schema under ``waba_id``: the cached id, else
    create + publish + store a new Flow. Every step is loud — a create, publish, or
    store failure raises and never falls back to another answer format.

    A publish or cache failure AFTER a successful create strands the draft on Meta,
    and the central retry re-enters create under the same name — so the draft is
    best-effort deleted before re-raising the original error; a delete that itself
    fails is logged without masking it.
    """
    cached = await get_cached_flow_id(waba_id, schema_hash)
    if cached is not None:
        return cached
    flow_id = await create_flow(waba_id=waba_id, name=f"{_FLOW_NAME_PREFIX}{schema_hash}", flow_json=flow_json)
    try:
        await publish_flow(flow_id)
        await cache_flow_id(waba_id, schema_hash, flow_id)
    except Exception:
        try:
            await delete_flow(flow_id)
        except Exception:
            logger.exception("failed to delete orphaned draft flow %s after resolve failure", flow_id)
        raise
    return flow_id


async def _deliver_form(
    settings: WhatsAppSettings, phone_number_id: str, target: str, delivery: ChannelDelivery
) -> None:
    """Deliver a form ask as a WhatsApp Flow (Tier-2: reserve BEFORE send).

    The Flow is built and its schema validated BEFORE any network work — an
    unsupported schema raises here, before the ``CHANNEL_WHATSAPP_WABA_ID`` gate,
    the reservation, or a send. The reservation carries the answer schema (so an
    inbound Flow response is coerced to its types) and the question text (so a
    door-rejected answer is re-asked with a fresh Flow), and uses the
    ``interaction_id`` as the ``flow_token`` correlating the completed form. A
    failure resolving the
    Flow or sending releases the reservation and raises — never a fallback format.
    """
    if delivery.schema is None:
        raise ChannelDeliveryError(f"form delivery {delivery.interaction_id} is missing its schema")
    flow_json, schema_hash = build_flow(delivery.schema)
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")

    await reserve_pending(
        phone_number_id=phone_number_id,
        wa_id=target,
        callback_url=delivery.callback_url,
        timeout_at=delivery.timeout_at,
        interaction_id=delivery.interaction_id,
        schema=delivery.schema,
        question=delivery.question,
    )
    try:
        flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json)
        await send_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=delivery.question,
            flow_id=flow_id,
            flow_token=delivery.interaction_id,
        )
    except Exception:
        # Any create/publish/store/send failure frees the pair instead of holding
        # it until its TTL.
        await release_pending(phone_number_id=phone_number_id, wa_id=target)
        raise


def _render_link(delivery: ChannelDelivery) -> str:
    """The message body for a Tier-1 ask (``confirm`` or ``external``): the
    question plus the tappable callback link."""
    return f"{delivery.question}\n\nAnswer here: {delivery.callback_url}"


def _numbered_options(delivery: ChannelDelivery) -> str:
    """The numbered-text fallback body for a select ask past the interactive caps."""
    lines = [delivery.question]
    lines.extend(f"{index}. {option}" for index, option in enumerate(delivery.options or [], start=1))
    lines.append("Reply with the text of one option.")
    return "\n".join(lines)


def _interaction_ids(delivery: ChannelDelivery) -> list[tuple[str, str]]:
    """``(id, title)`` per option, id = ``{interaction_id}:{index}`` (0-based).

    The index binds the tap to the exact ask: the inbound handler requires the
    id's interaction part to equal the pending ask's before mapping the index to
    ``options[index]``.
    """
    return [(f"{delivery.interaction_id}:{index}", option) for index, option in enumerate(delivery.options or [])]


def _select_send_kind(delivery: ChannelDelivery) -> str:
    """Which native shape a select ask renders as: ``"buttons"``, ``"list"``, or
    ``"fallback"`` (numbered text) when it fits neither interactive shape."""
    options = delivery.options or []
    if len(delivery.question) > _INTERACTIVE_BODY_MAX_CHARS:
        return "fallback"
    if (
        len(options) <= _BUTTON_MAX_COUNT
        and all(len(option) <= _BUTTON_TITLE_MAX_CHARS for option in options)
        # Reply-button titles must be unique; duplicate option text falls to a list.
        and len(set(options)) == len(options)
    ):
        return "buttons"
    if len(options) <= _LIST_MAX_ROWS and all(len(option) <= _LIST_ROW_TITLE_MAX_CHARS for option in options):
        return "list"
    return "fallback"


async def _send_question(phone_number_id: str, target: str, delivery: ChannelDelivery) -> None:
    """Push a Tier-2 question to the target in its native shape."""
    if delivery.answer_format != "select":
        await send_message(phone_number_id=phone_number_id, to=target, body=delivery.question)
        return
    kind = _select_send_kind(delivery)
    if kind == "buttons":
        await send_interactive_buttons(
            phone_number_id=phone_number_id, to=target, body=delivery.question, buttons=_interaction_ids(delivery)
        )
    elif kind == "list":
        await send_interactive_list(
            phone_number_id=phone_number_id,
            to=target,
            body=delivery.question,
            button_text=_LIST_BUTTON_LABEL,
            rows=_interaction_ids(delivery),
        )
    else:
        await send_message(phone_number_id=phone_number_id, to=target, body=_numbered_options(delivery))


def _require_recipient(requested: str | None, message: str) -> str:
    """The caller-supplied recipient, or raise ``ChannelDeliveryError`` — this
    channel has no operator default recipient."""
    if requested is None:
        raise ChannelDeliveryError(message)
    return requested


_NO_DEFAULT_RECIPIENT = "no recipient requested and this channel has no default recipient; request a wa_id"


async def _resolve_template_target(settings: WhatsAppSettings, phone_number_id: str, requested: str | None) -> str:
    """The recipient for a TEMPLATE send: required, and on the allowlist OR a
    known contact of the send-from ``phone_number_id``, else refused loudly.

    A template is the one send Meta delivers cold, so it keeps an operator fence;
    the known-contact lookup keys on the resolved send-from number — a guest is
    "known" to the number they actually messaged.
    """
    target = _require_recipient(requested, _NO_DEFAULT_RECIPIENT)
    if target in set(settings.allowed_recipients):
        return target
    if await is_known_contact(phone_number_id, target):
        return target
    raise ChannelDeliveryError(
        f"template send to {target!r} refused: not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS and not a known "
        f"contact of {phone_number_id} within the configured window"
    )


def _link_line(item: MediaItem) -> str:
    """A ``link`` media item rendered as one appended body line."""
    return f"{item.caption}: {item.url}" if item.caption else item.url


async def _send_body_and_media(phone_number_id: str, target: str, notification: ChannelNotification) -> list[str]:
    """Send the freeform body (with any ``link`` items appended) then each
    ``image`` item as its own image message; return every ``wamid`` in send order.

    A ``data:`` image URL is refused loudly BEFORE anything is sent — Meta's
    link-sourced image send takes a public URL only, so a data URI would fail
    Meta-side; refusing here names the constraint. A part that fails mid-send
    raises naming the wamids already delivered (partial delivery stays visible).
    """
    media = notification.media or []
    images = [item for item in media if item.kind == MediaKind.IMAGE]
    links = [item for item in media if item.kind == MediaKind.LINK]

    for image in images:
        if image.url.startswith("data:"):
            raise ChannelDeliveryError(
                f"WhatsApp cannot send a data: image URL ({image.url[:32]}...); a link-sourced image "
                "requires a public https URL"
            )

    body = notification.message
    if links:
        body = "\n".join([body, *(_link_line(item) for item in links)])

    sent: list[str] = [await send_message(phone_number_id=phone_number_id, to=target, body=body)]
    for image in images:
        try:
            sent.append(
                await send_image(phone_number_id=phone_number_id, to=target, link=image.url, caption=image.caption)
            )
        except ChannelDeliveryError as exc:
            raise ChannelDeliveryError(f"WhatsApp multi-part send failed after delivering {sent}: {exc}") from exc
    return sent


async def _send_template(
    settings: WhatsAppSettings, phone_number_id: str, target: str, template: ChannelTemplate
) -> list[str]:
    """Enforce the template recipient policy, then send the template."""
    resolved = await _resolve_template_target(settings, phone_number_id, target)
    return [await send_template(phone_number_id=phone_number_id, to=resolved, template=template)]


class WhatsAppChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol."""

    # This channel sends images and out-of-window templates; the central
    # notify_user capability guard reads these before dispatching either.
    supports_media_notifications: ClassVar[bool] = True
    supports_template_notifications: ClassVar[bool] = True
    # This channel renders a form ask as a WhatsApp Flow; the ask_user helper reads
    # this before handing a form delivery over.
    supports_form_delivery: ClassVar[bool] = True

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Resolve the destination ``wa_id``, then push the question to it.

        The question is a freeform send, so the recipient is required but not
        allowlist-fenced (Meta's 24-hour window is the fence). An ask already past
        its deadline is refused loudly for every format before any reservation or
        send. A select ask renders in its native shape (buttons/list/numbered
        text); the reservation carries the ask's options and interaction id so an
        interactive tap resolves back to the exact option text.
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
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any
        failure. Returns every ``wamid`` the Cloud API assigned, in send order.

        No reply is expected, so nothing touches the correlation store. Exactly one
        send attempt per part (a plain return means Meta ACCEPTED it, not that a
        human saw it). ``sender_identity`` set → send FROM that ``phone_number_id``;
        unset → the configured ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID``. When
        ``template`` is set the send is the out-of-window template (recipient
        allowlist-or-known-contact); otherwise the freeform body plus any media
        parts (freeform recipient unfenced). The recipient is always required.
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
        return await _send_body_and_media(phone_number_id, target, notification)
