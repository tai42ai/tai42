"""Outbound WhatsApp (Meta Graph) send.

One endpoint: ``POST {api}/{phone_number_id}/messages``, Bearer auth, JSON body,
via the kit's pooled ``HttpxClient``. Exactly ONE send attempt per message — a
blind retry of an ambiguous failure risks messaging the human twice. Any failure
raises ``ChannelDeliveryError``.

One builder per message shape (text, interactive buttons, interactive list,
image, template) assembles the payload and hands it to the single ``_post`` send
seam (auth, transport, error mapping, wamid extraction); each returns the send's
``wamid`` (``messages[0].id``).
"""

from __future__ import annotations

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, ChannelTemplate
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_whatsapp.settings import require_delivery_secret, whatsapp_settings


async def _post(phone_number_id: str, payload: dict[str, object]) -> str:
    """Send one already-built message payload FROM ``phone_number_id``; return its
    ``wamid`` (``messages[0].id``).

    The single send seam for every message shape. Raises ``ChannelDeliveryError``
    on a missing access token (checked before any network work), transport
    failure, a non-2xx response, or a 2xx that carries no message id. Never retries.
    """
    settings = whatsapp_settings()
    access_token = require_delivery_secret(settings.access_token, "CHANNEL_WHATSAPP_ACCESS_TOKEN")
    url = f"{settings.api_base_url}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with tai42_app.clients.client_ctx(HttpxClient, timeout=settings.http_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"WhatsApp send failed in transport: {exc!r}") from exc
    if response.status_code not in (200, 201):
        raise ChannelDeliveryError(
            f"WhatsApp rejected the send: HTTP {response.status_code}: {_error_detail(response)}"
        )
    messages = response.json().get("messages") or []
    if not messages or not messages[0].get("id"):
        raise ChannelDeliveryError("WhatsApp accepted the send but returned no message id")
    return messages[0]["id"]


async def send_message(phone_number_id: str, to: str, body: str) -> str:
    """Send one WhatsApp text message; return its ``wamid``."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    return await _post(phone_number_id, payload)


async def send_interactive_buttons(phone_number_id: str, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
    """Send an interactive reply-buttons message; return its ``wamid``.

    ``buttons`` is a list of ``(id, title)`` pairs — each becomes a tappable reply
    button whose id the inbound webhook echoes back.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": bid, "title": title}} for bid, title in buttons]},
        },
    }
    return await _post(phone_number_id, payload)


async def send_interactive_list(
    phone_number_id: str, to: str, body: str, button_text: str, rows: list[tuple[str, str]]
) -> str:
    """Send an interactive list message (single section); return its ``wamid``.

    ``button_text`` labels the list-opening button; ``rows`` is a list of
    ``(id, title)`` pairs — each a selectable row whose id the inbound webhook
    echoes back.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text,
                "sections": [{"rows": [{"id": rid, "title": title} for rid, title in rows]}],
            },
        },
    }
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


async def send_template(phone_number_id: str, to: str, template: ChannelTemplate) -> str:
    """Send a pre-approved template message; return its ``wamid``.

    Maps the template's positional ``parameters`` onto Meta's single ``body``
    component as ordered ``text`` parameters; a template with no parameters sends
    with no components.
    """
    template_obj: dict[str, object] = {"name": template.name, "language": {"code": template.language}}
    if template.parameters:
        template_obj["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": value} for value in template.parameters]}
        ]
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template_obj}
    return await _post(phone_number_id, payload)


def _error_detail(response: httpx.Response) -> str:
    """Meta's ``error.code``/``error.message`` when the body is JSON, else raw
    text; bounded to 500 chars so an HTML error page cannot flood the exception."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    return f"code={error.get('code')} message={error.get('message')!r}"[:500]
