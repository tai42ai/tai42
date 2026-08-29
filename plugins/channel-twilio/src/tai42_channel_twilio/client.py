"""Outbound Twilio REST send.

One endpoint: ``POST {api}/Accounts/{AccountSid}/Messages.json``, HTTP Basic auth
(``AccountSid:AuthToken``), form-encoded body, via the kit's pooled ``HttpxClient``.
Exactly ONE send attempt per message — the Messages API has no idempotency key, so
retrying an ambiguous failure risks texting the human twice. Any failure raises
``ChannelDeliveryError``.
"""

from __future__ import annotations

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_twilio.settings import require_delivery_secret, require_delivery_setting, twilio_settings


async def send_message(to: str, from_number: str, body: str, media_urls: list[str] | None = None) -> str:
    """Send one SMS/MMS/WhatsApp message; return its ``MessageSid``.

    ``media_urls`` (when present, non-empty) attach as MMS/WhatsApp media: each is a
    repeated ``MediaUrl`` form field Twilio fetches and delivers alongside the
    ``Body``. Twilio requires a publicly fetchable url per item (the channel refuses a
    ``data:`` image before calling here) and bounds one message's media count — a send
    past that bound is Twilio's own loud rejection below, never a silent drop.

    ``body`` may be EMPTY for a media-only send (a caption-less image): the ``Body`` field is
    then OMITTED and the message is a body-less MMS carrying only its ``MediaUrl`` — Twilio
    accepts that when media is present. Every text send passes a non-blank ``body``.

    Raises ``ChannelDeliveryError`` on missing credentials (checked before any
    network work), transport failure, or a non-2xx response. Never retries.
    """
    settings = twilio_settings()
    account_sid = require_delivery_setting(settings.account_sid, "CHANNEL_TWILIO_ACCOUNT_SID")
    auth_token = require_delivery_secret(settings.auth_token, "CHANNEL_TWILIO_AUTH_TOKEN")
    url = f"{settings.api_base_url}/Accounts/{account_sid}/Messages.json"
    form: dict[str, str | list[str]] = {"To": to, "From": from_number}
    if body:
        form["Body"] = body
    if media_urls:
        # httpx encodes a list value as a repeated ``MediaUrl`` field — Twilio's wire
        # shape for multiple media on one message.
        form["MediaUrl"] = media_urls
    try:
        async with tai42_app.clients.client_ctx(HttpxClient, timeout=settings.http_timeout_seconds) as client:
            response = await client.post(url, data=form, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"Twilio send failed in transport: {exc!r}") from exc
    if response.status_code not in (200, 201):
        raise ChannelDeliveryError(f"Twilio rejected the send: HTTP {response.status_code}: {_error_detail(response)}")
    sid = response.json().get("sid")
    if not sid:
        raise ChannelDeliveryError("Twilio accepted the send but returned no MessageSid")
    return sid


def _error_detail(response: httpx.Response) -> str:
    """Twilio's ``code``/``message`` when the body is JSON, else raw text; bounded
    to 500 chars so an HTML error page cannot flood the exception."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    return f"code={payload.get('code')} message={payload.get('message')!r}"[:500]
