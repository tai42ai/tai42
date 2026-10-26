"""The Meta Graph transport and error policy every WhatsApp Graph call shares.

:func:`send_request` sends one already-built request with Bearer auth through the
kit's pooled ``HttpxClient`` and maps a transport fault or a non-2xx answer to a
``ChannelDeliveryError`` carrying the transient/hard classification and Meta's
``Retry-After``. It never retries; the central caller decides.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_whatsapp.settings import require_delivery_secret, whatsapp_settings

# Meta ``error.code`` values that mean throttled, not refused: the app request
# limit (4), the rate limit hit (80007), the throughput limit (130429), the spam
# rate limit (131048), and the per-pair rate limit (131056).
_RATE_LIMIT_CODES = frozenset({4, 80007, 130429, 131048, 131056})


async def send_request(
    url: str,
    payload: dict[str, object] | None = None,
    *,
    method: str = "post",
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Send one already-built request to a graph ``url`` and return its 2xx response.

    The single auth + transport + error-classification seam shared by every Graph call.
    ``method`` is ``"post"`` (JSON ``payload`` body), ``"get"`` (query ``params``, no
    body) or ``"delete"`` (no body). Raises ``ChannelDeliveryError`` on a missing access
    token (checked before any network work), transport failure, or a non-2xx response.
    Never retries: the raised error carries the transient/hard classification the
    central caller retries on. Body extraction (wamid, flow id, list page) is the caller's.
    """
    settings = whatsapp_settings()
    access_token = require_delivery_secret(settings.access_token, "CHANNEL_WHATSAPP_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with tai42_app.clients.client_ctx(HttpxClient, timeout=settings.http_timeout_seconds) as client:
            if method == "get":
                response = await client.get(url, params=params, headers=headers)
            elif method == "delete":
                response = await client.delete(url, headers=headers)
            else:
                response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # A transport fault may have landed Meta-side; a re-send is accepted —
        # both sends carry the same reply correlation.
        raise ChannelDeliveryError(f"WhatsApp send failed in transport: {exc!r}", retryable=True) from exc
    if response.status_code not in (200, 201):
        body = parse_json(response)  # parsed once: the detail and the classification share it
        retryable, retry_after = _retry_policy(response, body)
        raise ChannelDeliveryError(
            f"WhatsApp rejected the send: HTTP {response.status_code}: {_error_detail(response, body)}",
            retryable=retryable,
            retry_after=retry_after,
        )
    return response


def parse_json(response: httpx.Response) -> Any:
    """The response body parsed as JSON, or ``None`` when it carries none."""
    try:
        return response.json()
    except ValueError:
        return None


# Meta's Graph ``error`` object fields, in the vendor's documented order — every
# one the operator's diagnostic needs, none dropped.
_ERROR_FIELDS = (
    "code",
    "error_subcode",
    "type",
    "message",
    "error_user_title",
    "error_user_msg",
    "error_data",
    "fbtrace_id",
)


def _error_detail(response: httpx.Response, payload: Any) -> str:
    """Meta's full documented ``error`` object from the parsed body, else raw text.

    Each present field renders as a ``name=<render>`` token in the vendor's fixed
    order — ``repr(value)`` for a scalar, compact sorted JSON for ``error_data`` —
    joined by one space; the whole is bounded to 500 chars so an HTML error page
    cannot flood the exception. A non-JSON body, or an ``error`` that is absent or
    not a dict, falls back to the raw response text (also bounded).
    """
    if not isinstance(payload, dict):
        return response.text[:500]
    error = payload.get("error")
    if not isinstance(error, dict):
        return response.text[:500]
    tokens: list[str] = []
    for name in _ERROR_FIELDS:
        if name not in error:
            continue
        value = error[name]
        if isinstance(value, (list, dict)):
            render = json.dumps(value, sort_keys=True, separators=(",", ":"))
        else:
            render = repr(value)
        tokens.append(f"{name}={render}")
    return " ".join(tokens)[:500]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """The ``Retry-After`` header as seconds, or ``None`` when absent or in HTTP-date form.

    For the HTTP-date form the backoff stands in for it.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _retry_policy(response: httpx.Response, payload: Any) -> tuple[bool, float | None]:
    """Whether a rejected send may be re-attempted, and the seconds Meta asked the caller to wait.

    A ``Retry-After`` header is honored on every transient rejection, not only the
    429.

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
