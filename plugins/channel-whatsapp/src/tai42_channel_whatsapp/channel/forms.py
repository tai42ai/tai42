"""WhatsApp-Flow form send: the form ask (ask delivery) and the ask-less notify.

Both build and validate the per-send Flow and its data before any network work,
resolve one published Flow per ``(schema, pages, option_fields)`` triple (created
and cached under the WABA id), and send the Flow message. The ask reserves a
correlation; the ask-less notify rides a ``tai42-nf:`` token instead.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification

from tai42_channel_whatsapp.channel.media import _send_media_prelude
from tai42_channel_whatsapp.client import create_flow, delete_flow, publish_flow, send_flow
from tai42_channel_whatsapp.correlation import (
    cache_flow_id,
    cache_flow_schema,
    get_cached_flow_id,
    release_pending,
    reserve_pending,
)
from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow
from tai42_channel_whatsapp.settings import WhatsAppSettings, require_delivery_setting

logger = logging.getLogger(__name__)

# The flow-token namespace for an ASK-LESS form notification: prefix + schema hash
# + a random suffix. Inbound routing branches on this prefix BEFORE any pending-ask
# lookup, so a notify-form submission can never answer (or disturb) a question
# pending on the same pair — while an ask's flow token stays its interaction id
# verbatim and never enters this namespace. The prefix is wire-visible in every
# delivered form's token; changing it orphans the forms already sitting in chats.
_NOTIFY_FORM_TOKEN_PREFIX = "tai42-nf:"

# A Flow's name on Meta, the schema-hash suffix making it a deterministic label
# for operator legibility — NOT a uniqueness key: Meta does not enforce flow-name
# uniqueness. One Flow per distinct answer schema comes from the cache plus the
# orphan-draft cleanup, never from the name.
_FLOW_NAME_PREFIX = "tai42-form-"

# The entry screen a stepped form's send navigates to (``build_form_flow``'s first
# screen); the send injects the per-send values/options as its ``data``.
_FORM_ENTRY_SCREEN = "SCREEN_0"


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


def _form_pages_list(send: ChannelDelivery | ChannelNotification) -> list[dict[str, Any]] | None:
    """The form's step layout as plain JSON — each page ``{"title", "fields"}`` — or
    ``None`` when the send carried one page. Shared by the form ask and the ask-less form
    notification, whose ``pages`` field is the identical shape."""
    if send.pages is None:
        return None
    return [{"title": page.title, "fields": list(page.fields)} for page in send.pages]


def _form_values_and_options(
    send: ChannelDelivery | ChannelNotification,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The form's per-send ``values`` and ``options`` as plain JSON — each option
    ``{"value", "label"?}`` (label omitted when absent). Empty when the send carried no
    data. Shared by the form ask and the ask-less form notification, whose ``data`` field
    is the identical shape."""
    if send.data is None:
        return {}, {}
    options: dict[str, list[dict[str, Any]]] = {}
    for name, choices in send.data.options.items():
        options[name] = [
            {"value": choice.value, **({"label": choice.label} if choice.label is not None else {})}
            for choice in choices
        ]
    return dict(send.data.values), options


async def _deliver_form(
    settings: WhatsAppSettings, phone_number_id: str, target: str, delivery: ChannelDelivery
) -> None:
    """Deliver a form ask as a WhatsApp Flow (Tier-2: reserve BEFORE send).

    The Flow (one screen per page) and its per-send data are built and validated
    BEFORE any network work — an unsupported schema, an unmappable per-send option, or
    an unknown page field raises here, before the ``CHANNEL_WHATSAPP_WABA_ID`` gate, the
    reservation, or a send. The reservation carries the answer schema (so an inbound
    Flow response is coerced to its types) and the question text (so a door-rejected
    answer is re-asked), and uses the ``interaction_id`` as the ``flow_token``
    correlating the completed form. The published Flow is keyed by the
    ``(schema, pages, option_fields)`` triple (the option-bearing fields decide which
    string properties render as dropdowns) and REUSED across sends; the prefilled values
    and per-send option lists ride the send's ``flow_action_payload.data`` (a dynamic
    data-source), never a new Flow.
    A failure resolving the Flow or sending releases the reservation and raises — never a
    fallback format.
    """
    if delivery.schema is None:
        raise ChannelDeliveryError(f"form delivery {delivery.interaction_id} is missing its schema")
    pages = _form_pages_list(delivery)
    values, options = _form_values_and_options(delivery)
    flow_json, schema_hash = build_form_flow(delivery.schema, pages, set(options))
    flow_data = build_flow_data(delivery.schema, values, options)
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
            screen=_FORM_ENTRY_SCREEN,
            data=flow_data,
        )
    except Exception:
        # Any create/publish/store/send failure frees the pair instead of holding
        # it until its TTL.
        await release_pending(phone_number_id=phone_number_id, wa_id=target)
        raise


async def _send_form_notification(
    settings: WhatsAppSettings, phone_number_id: str, target: str, notification: ChannelNotification
) -> list[str]:
    """Send an ask-less form notification: any display media as the standard prelude
    (the ``link`` items as one text line-block, each ``image`` as its own message),
    then the Flow message LAST — the actionable prompt stays at the foot of the chat.
    Returns every ``wamid`` in send order.

    The Flow is resolved exactly like a form ask's (one published Flow per answer
    schema, cached under the WABA id), and the answer schema itself is cached beside
    the flow id — the submission's reply carries only the schema hash inside its
    flow token, so that sidecar is the ONLY place the inbound side can recover the
    schema to coerce the values (see :func:`cache_flow_schema`). NO correlation is
    reserved: the token — minted in the ``tai42-nf:`` namespace as prefix + schema
    hash + a random suffix — routes the reply, not the pair, so any number of forms
    may be outstanding and a pending ask on the same pair is never touched.
    """
    if notification.schema is None:  # dispatch guard; notify() branches on the field
        raise ChannelDeliveryError("form notification is missing its schema")
    # Mirror the form ask: the per-send Flow (one screen per page) and its prefill/option
    # data are built and validated BEFORE any network work, and the send navigates to the
    # entry screen injecting that data — so an ask-less form opens already filled in. The
    # published Flow is keyed by the ``(schema, pages, option_fields)`` triple and reused
    # across sends; the prefilled values and per-send option lists ride the send's data.
    pages = _form_pages_list(notification)
    values, options = _form_values_and_options(notification)
    flow_json, schema_hash = build_form_flow(notification.schema, pages, set(options))
    flow_data = build_flow_data(notification.schema, values, options)
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")

    sent = await _send_media_prelude(phone_number_id, target, list(notification.media or []))
    flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json)
    # Written beside every flow-id use — the reply side cannot repopulate it.
    await cache_flow_schema(waba_id, schema_hash, notification.schema)
    flow_token = f"{_NOTIFY_FORM_TOKEN_PREFIX}{schema_hash}:{uuid4().hex}"
    sent.append(
        await send_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=notification.message,
            flow_id=flow_id,
            flow_token=flow_token,
            screen=_FORM_ENTRY_SCREEN,
            data=flow_data,
        )
    )
    return sent
