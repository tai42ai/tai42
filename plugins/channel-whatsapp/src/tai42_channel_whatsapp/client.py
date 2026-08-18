"""Outbound WhatsApp (Meta Graph) send.

One endpoint: ``POST {api}/{phone_number_id}/messages``, Bearer auth, JSON body,
via the kit's pooled ``HttpxClient``. Exactly ONE send attempt per message — no
loop here; the raised ``ChannelDeliveryError`` carries ``retryable`` (and Meta's
``Retry-After`` when it sent one) so the central caller decides. Any failure
raises ``ChannelDeliveryError``.

One builder per message shape (text, interactive buttons, interactive list,
image, template, flow) assembles the payload and hands it to the single ``_post``
send seam (auth, transport, error mapping, wamid extraction); each returns the
send's ``wamid`` (``messages[0].id``). The Flow lifecycle builders
(``create_flow``, ``publish_flow``, ``delete_flow``) share the same auth +
transport + error policy through ``_send`` but target the graph object
endpoints, not ``/messages``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, ChannelTemplate
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


async def send_flow(phone_number_id: str, to: str, body_text: str, flow_id: str, flow_token: str) -> str:
    """Send an interactive Flow message opening the published ``flow_id``; return
    its ``wamid``.

    ``flow_token`` correlates the completed form back to the pending question (it
    is the delivery's ``interaction_id``). The send navigates to the flow's single
    terminal screen; the human fills it and the completed payload returns inbound
    as an ``nfm_reply`` carrying this token.
    """
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
                    "flow_action_payload": {"screen": "FORM"},
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
