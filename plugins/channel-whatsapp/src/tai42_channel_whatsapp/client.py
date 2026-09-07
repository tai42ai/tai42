"""Outbound WhatsApp (Meta Graph) send.

One endpoint: ``POST {api}/{phone_number_id}/messages``, Bearer auth, JSON body,
via the kit's pooled ``HttpxClient``. Exactly ONE send attempt per message — no
loop here; the raised ``ChannelDeliveryError`` carries ``retryable`` (and Meta's
``Retry-After`` when it sent one) so the central caller decides. Any failure
raises ``ChannelDeliveryError``.

One builder per message shape (text; interactive buttons / list / cta_url; image,
document, video, audio, location; template; flow) assembles the payload and hands
it to the single ``_post`` send seam (auth, transport, error mapping, wamid
extraction); each returns the send's ``wamid`` (``messages[0].id``). The
interactive builders take an optional pre-built media ``header`` and text
``footer``; the template builder maps the template's named components
(``header_media`` / ``body_parameters`` / ``buttons``). The Flow lifecycle builders
(``create_flow``, ``publish_flow``, ``delete_flow``) share the same auth +
transport + error policy through ``_send`` but target the graph object
endpoints, not ``/messages``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    ChannelDeliveryError,
    ChannelInputError,
    ChannelTemplate,
    QuickReplyButtonParam,
    TemplateButtonParam,
)
from tai42_contract.interactions.models import MediaItem, MediaKind
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_whatsapp.settings import require_delivery_secret, whatsapp_settings

# Meta ``error.code`` values that mean throttled, not refused: the app request
# limit (4), the rate limit hit (80007), the throughput limit (130429), the spam
# rate limit (131048), and the per-pair rate limit (131056).
_RATE_LIMIT_CODES = frozenset({4, 80007, 130429, 131048, 131056})


async def _send(url: str, payload: dict[str, object] | None = None, *, method: str = "post") -> httpx.Response:
    """Send one already-built payload to a graph ``url`` and return its 2xx response.

    The single auth + transport + error-classification seam shared by every send.
    ``method`` is ``"post"`` (JSON ``payload`` body) or ``"delete"`` (no body).
    Raises ``ChannelDeliveryError`` on a missing access token (checked before any
    network work), transport failure, or a non-2xx response. Never retries: the
    raised error carries the transient/hard classification the central caller
    retries on. Body extraction (wamid, flow id) is the caller's.
    """
    settings = whatsapp_settings()
    access_token = require_delivery_secret(settings.access_token, "CHANNEL_WHATSAPP_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with tai42_app.clients.client_ctx(HttpxClient, timeout=settings.http_timeout_seconds) as client:
            if method == "delete":
                response = await client.delete(url, headers=headers)
            else:
                response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # A transport fault may have landed Meta-side; a re-send is accepted —
        # both sends carry the same reply correlation.
        raise ChannelDeliveryError(f"WhatsApp send failed in transport: {exc!r}", retryable=True) from exc
    if response.status_code not in (200, 201):
        body = _parse_json(response)  # parsed once: the detail and the classification share it
        retryable, retry_after = _retry_policy(response, body)
        raise ChannelDeliveryError(
            f"WhatsApp rejected the send: HTTP {response.status_code}: {_error_detail(response, body)}",
            retryable=retryable,
            retry_after=retry_after,
        )
    return response


async def _post(phone_number_id: str, payload: dict[str, object]) -> str:
    """Send one already-built message payload FROM ``phone_number_id``; return its
    ``wamid`` (``messages[0].id``).

    The send seam for every message shape. Raises ``ChannelDeliveryError`` on the
    ``_send`` failure modes or a 2xx that carries no message id.
    """
    url = f"{whatsapp_settings().api_base_url}/{phone_number_id}/messages"
    response = await _send(url, payload)
    messages = response.json().get("messages") or []
    if not messages or not messages[0].get("id"):
        raise ChannelDeliveryError("WhatsApp accepted the send but returned no message id")
    return messages[0]["id"]


async def mark_read_typing(phone_number_id: str, wamid: str) -> None:
    """Mark inbound ``wamid`` read and show a typing indicator to its sender.

    POSTs the combined mark-as-read + typing-indicator body to
    ``{api}/{phone_number_id}/messages`` (Graph v23.0). Meta answers
    ``{"success": true}`` with no ``messages[].id``, so this rides ``_send``
    directly and discards the body — ``_post`` is unusable here (it demands a
    returned message id). Raises ``ChannelDeliveryError`` on the ``_send`` failure
    modes; the inbound caller swallows it — a typing hint must never fail a batch.
    """
    url = f"{whatsapp_settings().api_base_url}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wamid,
        "typing_indicator": {"type": "text"},
    }
    await _send(url, payload)


async def send_message(phone_number_id: str, to: str, body: str) -> str:
    """Send one WhatsApp text message; return its ``wamid``."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    return await _post(phone_number_id, payload)


def _header_object(header: MediaItem) -> dict[str, Any]:
    """The ``interactive.header`` object for a media header, per the Cloud API.

    A header carries a single ``image``/``video``/``document`` object (a ``document`` may
    add a ``filename``). WhatsApp interactive headers support text/image/video/document
    ONLY — an ``audio`` header has no representation here, so the caller sends the audio as
    its own message ahead of the interactive instead of reaching this builder (a ``link``
    is never a header by contract)."""
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
    """``interactive`` with an optional media ``header`` and text ``footer`` added — the two
    pure enhancements every interactive shape (buttons/list/cta_url) shares. Keys are added
    only when set, so a send without them is byte-identical to the plain interactive."""
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
) -> str:
    """Send an interactive reply-buttons message; return its ``wamid``.

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
) -> str:
    """Send an interactive list message; return its ``wamid``.

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
) -> str:
    """Send an interactive call-to-action URL message (one URL button); return its ``wamid``.

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


async def send_image(phone_number_id: str, to: str, link: str, caption: str | None) -> str:
    """Send a link-sourced image message; return its ``wamid``.

    ``link`` must be a public https URL (Meta fetches it) — a ``data:`` URI is
    rejected upstream at the channel, never handed here.
    """
    image: dict[str, str] = {"link": link}
    if caption:
        image["caption"] = caption
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": image}
    return await _post(phone_number_id, payload)


async def send_document(phone_number_id: str, to: str, link: str, caption: str | None, filename: str | None) -> str:
    """Send a link-sourced document message; return its ``wamid``.

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


async def send_video(phone_number_id: str, to: str, link: str, caption: str | None) -> str:
    """Send a link-sourced video message; return its ``wamid``. ``caption`` optional."""
    video: dict[str, str] = {"link": link}
    if caption:
        video["caption"] = caption
    payload = {"messaging_product": "whatsapp", "to": to, "type": "video", "video": video}
    return await _post(phone_number_id, payload)


async def send_audio(phone_number_id: str, to: str, link: str) -> str:
    """Send a link-sourced audio message; return its ``wamid``.

    The Cloud API's ``audio`` object carries no caption or filename — a caption on an
    audio item is dropped at the channel (a pure enhancement the medium cannot render).
    """
    payload = {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"link": link}}
    return await _post(phone_number_id, payload)


async def send_location(
    phone_number_id: str, to: str, latitude: float, longitude: float, name: str | None, address: str | None
) -> str:
    """Send a location message; return its ``wamid``.

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
    """The template ``header`` component for a media header argument, per the template-message
    API: a single ``image``/``video``/``document`` parameter (a ``document`` may add a
    ``filename``). An ``audio`` header has no template representation and is refused loudly."""
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
    """One template ``button`` component for the i-th button's runtime argument, per the
    template-message API: a ``quick_reply`` carries a ``payload`` parameter, a ``url`` carries
    a ``text`` parameter (the dynamic suffix substituted into the button's pre-approved URL)."""
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


async def send_template(phone_number_id: str, to: str, template: ChannelTemplate) -> str:
    """Send a pre-approved template message; return its ``wamid``.

    Maps the template's NAMED components onto the Cloud API template-message ``components``
    array: a ``header`` component for ``header_media`` (image/video/document), a ``body``
    component whose ordered ``text`` parameters fill the body placeholders from
    ``body_parameters``, and one ``button`` component per ``buttons`` entry (a quick-reply
    ``payload`` or a url ``text`` suffix, positional by index). A template with no runtime
    arguments sends with no ``components`` key.
    """
    components: list[dict[str, Any]] = []
    if template.header_media is not None:
        components.append(_template_header_component(template.header_media))
    if template.body_parameters:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": value} for value in template.body_parameters]}
        )
    components.extend(_template_button_component(index, button) for index, button in enumerate(template.buttons))
    template_obj: dict[str, object] = {"name": template.name, "language": {"code": template.language}}
    if components:
        template_obj["components"] = components
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template_obj}
    return await _post(phone_number_id, payload)


async def create_flow(waba_id: str, name: str, flow_json: dict[str, Any]) -> str:
    """Create a Flow under ``waba_id`` from ``flow_json``; return its flow id.

    POSTs ``{name, categories: ["OTHER"], flow_json: <json string>}`` to
    ``{api}/{waba_id}/flows``. A 2xx that carries no ``id`` raises loudly (mirrors
    the no-message-id guard on the send path).
    """
    url = f"{whatsapp_settings().api_base_url}/{waba_id}/flows"
    payload = {"name": name, "categories": ["OTHER"], "flow_json": json.dumps(flow_json)}
    response = await _send(url, payload)
    flow_id = response.json().get("id")
    if not flow_id:
        raise ChannelDeliveryError("WhatsApp accepted the flow create but returned no flow id")
    return flow_id


async def publish_flow(flow_id: str) -> None:
    """Publish a draft Flow so it can be sent; POST ``{api}/{flow_id}/publish``."""
    url = f"{whatsapp_settings().api_base_url}/{flow_id}/publish"
    await _send(url, {})


async def delete_flow(flow_id: str) -> None:
    """Delete a draft Flow; DELETE ``{api}/{flow_id}``.

    Cleans up a draft stranded when a publish or cache write fails after create;
    Meta deletes only unpublished flows. Raises ``ChannelDeliveryError`` on
    failure like every builder — the caller decides whether to swallow it."""
    url = f"{whatsapp_settings().api_base_url}/{flow_id}"
    await _send(url, method="delete")


async def send_flow(
    phone_number_id: str,
    to: str,
    body_text: str,
    flow_id: str,
    flow_token: str,
    screen: str = "FORM",
    data: dict[str, Any] | None = None,
) -> str:
    """Send an interactive Flow message opening the published ``flow_id``; return
    its ``wamid``.

    ``flow_token`` correlates the completed form back to its origin: a form ask
    passes the delivery's ``interaction_id`` verbatim, an ask-less form
    notification a token in its own namespace. The send navigates to ``screen`` (the
    flow's entry screen); the human fills it and the completed payload returns
    inbound as an ``nfm_reply`` carrying this token.

    ``data`` is the per-send ``flow_action_payload.data`` — the prefilled values and
    the dynamic option data-sources a stepped/per-send form's screen reads. It is
    omitted from the payload when ``None`` (an ask-less form carries none), so a plain
    Flow send is byte-identical to before.
    """
    flow_action_payload: dict[str, Any] = {"screen": screen}
    if data is not None:
        flow_action_payload["data"] = data
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
                    "flow_cta": "Fill form",
                    "flow_action": "navigate",
                    "flow_action_payload": flow_action_payload,
                },
            },
        },
    }
    return await _post(phone_number_id, payload)


def _parse_json(response: httpx.Response) -> Any:
    """The response body parsed as JSON, or ``None`` when it carries none."""
    try:
        return response.json()
    except ValueError:
        return None


def _error_detail(response: httpx.Response, payload: Any) -> str:
    """Meta's ``error.code``/``error.message`` from the parsed body, else raw
    text; bounded to 500 chars so an HTML error page cannot flood the exception."""
    if not isinstance(payload, dict):
        return response.text[:500]
    error = payload.get("error")
    if not isinstance(error, dict):
        return response.text[:500]
    return f"code={error.get('code')} message={error.get('message')!r}"[:500]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """The ``Retry-After`` header as seconds, or ``None`` when absent or sent in
    the HTTP-date form (the backoff stands in for it)."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _retry_policy(response: httpx.Response, payload: Any) -> tuple[bool, float | None]:
    """Whether a rejected send may be re-attempted, and the seconds Meta asked the
    caller to wait for. A ``Retry-After`` header is honored on every transient
    rejection, not only the 429.

    Transient: any 5xx, an HTTP 429, or a Meta rate-limit ``error.code`` whatever
    the status. Everything else — a recipient outside the allowed list (131030),
    a send past the 24-hour window (131047), an expired token (190), any other
    4xx, an unreadable body — is a hard rejection a fresh attempt cannot fix.
    """
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    if response.status_code == 429 or response.status_code >= 500 or code in _RATE_LIMIT_CODES:
        return True, _retry_after_seconds(response)
    return False, None
