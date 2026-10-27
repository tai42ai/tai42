"""Outbound WhatsApp (Meta Graph) send.

One endpoint: ``POST {api}/{phone_number_id}/messages``, Bearer auth, JSON body,
via the kit's pooled ``HttpxClient``. Exactly ONE send attempt per message — no
loop here; the raised ``ChannelDeliveryError`` carries ``retryable`` (and Meta's
``Retry-After`` when it sent one) so the central caller decides. Any failure
raises ``ChannelDeliveryError``.

One builder per message shape (text; interactive buttons / list / cta_url; image,
document, video, audio, location; template; flow) assembles the payload and hands
it to the single ``_post`` send seam (auth, transport, error mapping, wamid
extraction); each returns the send's :class:`SentMessage`: its ``wamid``
(``messages[0].id``) and the recipient's ``wa_id`` (``contacts[0].wa_id``). The
interactive builders take an optional pre-built media ``header`` and text
``footer``; the template builder maps the template's named components
(``header_media`` / ``body_parameters`` / ``buttons``). The send seam's auth, transport and
error policy is :func:`~tai42_channel_whatsapp.graph.send_request`, which the Flow asset
wrappers (:mod:`tai42_channel_whatsapp.flow_assets`) and :func:`mark_read` share.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import ValidationError
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelTemplate,
    SendOutcome,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_channel_whatsapp.channel.template_params import (
    QuickReplyButtonParam,
    TemplateButtonParam,
    parse_template_parameters,
)
from tai42_channel_whatsapp.graph import send_request
from tai42_channel_whatsapp.settings import whatsapp_settings

if TYPE_CHECKING:
    pass


class SentMessage(NamedTuple):
    """One message the Cloud API accepted: its ``wamid`` and the ``wa_id`` it went to.

    ``wa_id`` is Meta's WhatsApp id of the recipient (``contacts[0].wa_id``), the same id an
    inbound message from them carries as ``from``; it may differ from the ``to`` the send named.
    """

    wamid: str
    wa_id: str


def send_outcome(phone_number_id: str, sent: list[SentMessage]) -> SendOutcome:
    """The :class:`SendOutcome` of the messages ``sent`` from ``phone_number_id``, in send order.

    The address is the first message's ``wa_id``, the ``client_address`` an inbound message
    from that recipient carries. Raises :class:`ChannelDeliveryError` when nothing was sent.
    """
    if not sent:
        raise ChannelDeliveryError("WhatsApp send produced no message")
    return SendOutcome(
        our_identity=phone_number_id,
        address=sent[0].wa_id,
        message_ids=[message.wamid for message in sent],
    )


async def _post(phone_number_id: str, payload: dict[str, object]) -> SentMessage:
    """Send one already-built message payload FROM ``phone_number_id``; return its :class:`SentMessage`.

    ``wamid`` is ``messages[0].id`` and ``wa_id`` is ``contacts[0].wa_id``. The send seam for
    every message shape. Raises ``ChannelDeliveryError`` on the ``send_request`` failure modes or a
    2xx that carries no message id or no contact ``wa_id``.
    """
    url = f"{whatsapp_settings().api_base_url}/{phone_number_id}/messages"
    response = await send_request(url, payload)
    body = response.json()
    messages = body.get("messages") or []
    if not messages or not messages[0].get("id"):
        raise ChannelDeliveryError("WhatsApp accepted the send but returned no message id")
    contacts = body.get("contacts") or []
    contact = contacts[0] if isinstance(contacts, list) and contacts else None
    wa_id = contact.get("wa_id") if isinstance(contact, dict) else None
    if not isinstance(wa_id, str) or not wa_id:
        raise ChannelDeliveryError(f"WhatsApp accepted the send ({messages[0]['id']}) but returned no contact wa_id")
    return SentMessage(wamid=messages[0]["id"], wa_id=wa_id)


async def mark_read(phone_number_id: str, wamid: str) -> None:
    """Mark inbound ``wamid`` read (the read receipt only, no typing indicator).

    POSTs the mark-as-read body to ``{api}/{phone_number_id}/messages`` (Graph
    v23.0). Meta answers ``{"success": true}`` with no ``messages[].id``, so this
    rides ``send_request`` directly and discards the body — ``_post`` is unusable here (it
    demands a returned message id). Raises ``ChannelDeliveryError`` on the ``send_request``
    failure modes; the inbound caller swallows it — a read receipt must never fail a
    batch.
    """
    url = f"{whatsapp_settings().api_base_url}/{phone_number_id}/messages"
    payload: dict[str, object] = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wamid,
    }
    await send_request(url, payload)


async def mark_read_typing(phone_number_id: str, wamid: str) -> None:
    """Mark inbound ``wamid`` read and show a typing indicator to its sender.

    POSTs the combined mark-as-read + typing-indicator body to
    ``{api}/{phone_number_id}/messages`` (Graph v23.0). Meta answers
    ``{"success": true}`` with no ``messages[].id``, so this rides ``send_request``
    directly and discards the body — ``_post`` is unusable here (it demands a
    returned message id). Re-marking read is idempotent, so the working-signal loop
    re-sends this to refresh the indicator, which Meta dismisses after 25 s or on the
    reply. Raises ``ChannelDeliveryError`` on the ``send_request`` failure modes.
    """
    url = f"{whatsapp_settings().api_base_url}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wamid,
        "typing_indicator": {"type": "text"},
    }
    await send_request(url, payload)


async def send_message(phone_number_id: str, to: str, body: str) -> SentMessage:
    """Send one WhatsApp text message; return its :class:`SentMessage`."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    return await _post(phone_number_id, payload)


def _header_object(header: MediaItem) -> dict[str, Any]:
    """The ``interactive.header`` object for a media header, per the Cloud API.

    A header carries a single ``image``/``video``/``document`` object (a ``document`` may
    add a ``filename``). WhatsApp interactive headers support text/image/video/document
    ONLY — an ``audio`` header has no representation here, so the caller sends the audio as
    its own message ahead of the interactive instead of reaching this builder (a ``link``
    is never a header by contract).
    """
    if header.kind is MediaKind.IMAGE:
        return {"type": "image", "image": {"link": header.url}}
    if header.kind is MediaKind.VIDEO:
        return {"type": "video", "video": {"link": header.url}}
    if header.kind is MediaKind.DOCUMENT:
        document: dict[str, str] = {"link": header.url}
        if header.filename:
            document["filename"] = header.filename
        return {"type": "document", "document": document}
    raise ChannelInputError(f"WhatsApp interactive header cannot carry {header.kind.value} media")


def _with_header_footer(
    interactive: dict[str, Any], header: dict[str, Any] | None, footer: str | None
) -> dict[str, Any]:
    """``interactive`` with an optional media ``header`` and text ``footer`` added.

    The two pure enhancements every interactive shape (buttons/list/cta_url)
    shares. Keys are added only when set, so a send without them is byte-identical
    to the plain interactive.
    """
    if header is not None:
        interactive["header"] = header
    if footer:
        interactive["footer"] = {"text": footer}
    return interactive


async def send_interactive_buttons(
    phone_number_id: str,
    to: str,
    body: str,
    buttons: list[tuple[str, str]],
    *,
    header: dict[str, Any] | None = None,
    footer: str | None = None,
) -> SentMessage:
    """Send an interactive reply-buttons message; return its :class:`SentMessage`.

    ``buttons`` is a list of ``(id, title)`` pairs — each becomes a tappable reply
    button whose id the inbound webhook echoes back. ``header`` is an optional pre-built
    media-header object (:func:`_header_object`) and ``footer`` an optional trailing line.
    """
    interactive = _with_header_footer(
        {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": bid, "title": title}} for bid, title in buttons]},
        },
        header,
        footer,
    )
    payload = {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive}
    return await _post(phone_number_id, payload)


async def send_interactive_list(
    phone_number_id: str,
    to: str,
    body: str,
    button_text: str,
    sections: list[dict[str, Any]],
    *,
    header: dict[str, Any] | None = None,
    footer: str | None = None,
) -> SentMessage:
    """Send an interactive list message; return its :class:`SentMessage`.

    ``button_text`` labels the list-opening button; ``sections`` is the Cloud-API
    ``action.sections`` array — one or more ``{title?, rows: [{id, title, description?}]}``
    groups, each row a selectable entry whose id the inbound webhook echoes back and whose
    optional ``description`` renders as the row's secondary line. ``header``/``footer`` are
    the optional media-header object and trailing line.
    """
    interactive = _with_header_footer(
        {"type": "list", "body": {"text": body}, "action": {"button": button_text, "sections": sections}},
        header,
        footer,
    )
    payload = {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive}
    return await _post(phone_number_id, payload)


async def send_interactive_cta_url(
    phone_number_id: str,
    to: str,
    body: str,
    display_text: str,
    url: str,
    *,
    header: dict[str, Any] | None = None,
    footer: str | None = None,
) -> SentMessage:
    """Send an interactive call-to-action URL message (one URL button); return its :class:`SentMessage`.

    The Cloud API's ``cta_url`` interactive carries exactly ONE URL button
    (``action.parameters = {display_text, url}``) — the single-link-option mapping; a tap
    OPENS the url and submits nothing. ``header``/``footer`` are the optional enhancements.
    """
    interactive = _with_header_footer(
        {
            "type": "cta_url",
            "body": {"text": body},
            "action": {"name": "cta_url", "parameters": {"display_text": display_text, "url": url}},
        },
        header,
        footer,
    )
    payload = {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive}
    return await _post(phone_number_id, payload)


async def send_image(phone_number_id: str, to: str, link: str, caption: str | None) -> SentMessage:
    """Send a link-sourced image message; return its :class:`SentMessage`.

    ``link`` must be a public https URL (Meta fetches it) — a ``data:`` URI is
    rejected upstream at the channel, never handed here.
    """
    image: dict[str, str] = {"link": link}
    if caption:
        image["caption"] = caption
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": image}
    return await _post(phone_number_id, payload)


async def send_document(
    phone_number_id: str, to: str, link: str, caption: str | None, filename: str | None
) -> SentMessage:
    """Send a link-sourced document message; return its :class:`SentMessage`.

    ``filename`` is the display name WhatsApp shows for the download (per the Cloud API's
    ``document`` object); ``caption`` an optional label. Both keys are added only when set.
    """
    document: dict[str, str] = {"link": link}
    if caption:
        document["caption"] = caption
    if filename:
        document["filename"] = filename
    payload = {"messaging_product": "whatsapp", "to": to, "type": "document", "document": document}
    return await _post(phone_number_id, payload)


async def send_video(phone_number_id: str, to: str, link: str, caption: str | None) -> SentMessage:
    """Send a link-sourced video message; return its :class:`SentMessage`. ``caption`` optional."""
    video: dict[str, str] = {"link": link}
    if caption:
        video["caption"] = caption
    payload = {"messaging_product": "whatsapp", "to": to, "type": "video", "video": video}
    return await _post(phone_number_id, payload)


async def send_audio(phone_number_id: str, to: str, link: str) -> SentMessage:
    """Send a link-sourced audio message; return its :class:`SentMessage`.

    The Cloud API's ``audio`` object carries no caption or filename — a caption on an
    audio item is dropped at the channel (a pure enhancement the medium cannot render).
    """
    payload = {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"link": link}}
    return await _post(phone_number_id, payload)


async def send_location(
    phone_number_id: str, to: str, latitude: float, longitude: float, name: str | None, address: str | None
) -> SentMessage:
    """Send a location message; return its :class:`SentMessage`.

    Maps a :class:`LocationElement` onto the Cloud API's ``location`` object
    (``latitude``/``longitude`` required; ``name``/``address`` added only when set).
    """
    location: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if name:
        location["name"] = name
    if address:
        location["address"] = address
    payload = {"messaging_product": "whatsapp", "to": to, "type": "location", "location": location}
    return await _post(phone_number_id, payload)


def _template_header_component(header: MediaItem) -> dict[str, Any]:
    """The template ``header`` component for a media header argument, per the template-message API.

    A single ``image``/``video``/``document`` parameter (a ``document`` may add a
    ``filename``). An ``audio`` header has no template representation and is refused
    loudly.
    """
    if header.kind is MediaKind.IMAGE:
        parameter: dict[str, Any] = {"type": "image", "image": {"link": header.url}}
    elif header.kind is MediaKind.VIDEO:
        parameter = {"type": "video", "video": {"link": header.url}}
    elif header.kind is MediaKind.DOCUMENT:
        document: dict[str, str] = {"link": header.url}
        if header.filename:
            document["filename"] = header.filename
        parameter = {"type": "document", "document": document}
    else:
        raise ChannelInputError(f"WhatsApp template header cannot carry {header.kind.value} media")
    return {"type": "header", "parameters": [parameter]}


def _template_button_component(index: int, button: TemplateButtonParam) -> dict[str, Any]:
    """One template ``button`` component for the i-th button's runtime argument, per the template-message API.

    A ``quick_reply`` carries a ``payload`` parameter, a ``url`` carries a ``text``
    parameter (the dynamic suffix substituted into the button's pre-approved URL).
    """
    if isinstance(button, QuickReplyButtonParam):
        return {
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(index),
            "parameters": [{"type": "payload", "payload": button.payload}],
        }
    return {
        "type": "button",
        "sub_type": "url",
        "index": str(index),
        "parameters": [{"type": "text", "text": button.url_parameter}],
    }


async def send_template(phone_number_id: str, to: str, template: ChannelTemplate) -> SentMessage:
    """Send a pre-approved template message; return its :class:`SentMessage`.

    The template's opaque ``parameters`` are parsed into this channel's own
    :class:`~tai42_channel_whatsapp.channel.template_params.WhatsAppTemplateParameters` (raising
    ``ChannelInputError`` on a shape this channel could not map) and mapped onto the Cloud API
    template-message ``components`` array: a ``header`` component for ``header_media``
    (image/video/document), a ``body`` component whose ordered ``text`` parameters fill the body
    placeholders from ``body_parameters``, and one ``button`` component per ``buttons`` entry (a
    quick-reply ``payload`` or a url ``text`` suffix, positional by index). A template with no
    runtime arguments sends with no ``components`` key.
    """
    try:
        params = parse_template_parameters(template.parameters)
    except (ValidationError, ValueError) as exc:
        raise ChannelInputError(f"WhatsApp template parameters are not renderable: {exc}") from exc
    components: list[dict[str, Any]] = []
    if params.header_media is not None:
        components.append(_template_header_component(params.header_media))
    if params.body_parameters:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": value} for value in params.body_parameters]}
        )
    components.extend(_template_button_component(index, button) for index, button in enumerate(params.buttons))
    template_obj: dict[str, object] = {"name": template.name, "language": {"code": template.language}}
    if components:
        template_obj["components"] = components
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template_obj}
    return await _post(phone_number_id, payload)


# The default label on the chat button that OPENS a Flow, made explicit; the caller's own
# ``wording.form.open`` overrides it per send.
_OPEN_LABEL = "Fill form"


async def send_flow(
    phone_number_id: str,
    to: str,
    body_text: str,
    flow_id: str,
    flow_token: str,
    screen: str,
    data: dict[str, Any],
    open_label: str | None = None,
) -> SentMessage:
    """Send an interactive Flow message opening the published ``flow_id``; return its :class:`SentMessage`.

    ``flow_token`` correlates the completed form back to its origin: a form ask
    passes the delivery's ``interaction_id`` verbatim, an ask-less form
    notification a token in its own namespace. The send navigates to ``screen`` (the
    flow's entry screen); the human fills it and the completed payload returns
    inbound as an ``nfm_reply`` carrying this token.

    ``data`` is the per-send ``flow_action_payload.data`` — the prefilled values and
    the dynamic option data-sources the entry screen reads; the builder always emits
    a ``data`` model, so every caller passes it.

    ``open_label`` is the caller's text for the chat button that opens the Flow (the
    sender's own words); absent, the generic :data:`_OPEN_LABEL` is drawn. It is a
    MESSAGE parameter (``flow_cta``), never part of the Flow JSON, so it never re-keys
    the published Flow.
    """
    flow_action_payload: dict[str, Any] = {"screen": screen, "data": data}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": body_text},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": open_label if open_label is not None else _OPEN_LABEL,
                    "flow_action": "navigate",
                    "flow_action_payload": flow_action_payload,
                },
            },
        },
    }
    return await _post(phone_number_id, payload)
