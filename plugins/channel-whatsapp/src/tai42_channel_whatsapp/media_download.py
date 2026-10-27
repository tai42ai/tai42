"""Inbound media download off the Graph media endpoint.

:func:`fetch_media_metadata` looks up an inbound media object's short-lived lookaside ``url``;
:func:`open_media_download` streams that ``url``'s bytes with the Bearer access token;
:func:`media_endpoint_host` is the endpoint's host for error and log text. The token rides the
request header only, never an error string.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.net import MediaFetchError, open_media_stream

from tai42_channel_whatsapp.settings import require_delivery_secret, whatsapp_settings

if TYPE_CHECKING:
    from tai42_kit.net import MediaStream


def media_endpoint_host() -> str:
    """The Graph media endpoint's host — error/log text only, never a URL or credential."""
    return httpx.URL(whatsapp_settings().api_base_url).host or "<unknown host>"


async def fetch_media_metadata(media_id: str) -> dict[str, Any]:
    """Look up an inbound media object's download metadata off the Graph media endpoint.

    ``GET {api_base_url}/{media_id}`` with the Bearer access token, returning Meta's metadata
    JSON — a short-lived, Bearer-authenticated lookaside ``url`` (guaranteed a non-empty string on
    return) plus ``mime_type``, ``sha256``, and ``file_size``. The token rides the request header
    only, never an error string. A transport fault, a 5xx, a 408 or a 429 is a TRANSIENT
    :class:`~tai42_kit.net.MediaFetchError` (a redelivery may succeed); any other 4xx (the media
    object has expired or is unknown), a body that is not a JSON object, or a body with no usable
    ``url`` is a PERMANENT one. The fault carries the vendor host and status only, never the url.
    """
    settings = whatsapp_settings()
    access_token = require_delivery_secret(settings.access_token, "CHANNEL_WHATSAPP_ACCESS_TOKEN")
    url = f"{settings.api_base_url}/{media_id}"
    host = httpx.URL(url).host or "<unknown host>"
    headers = {"Authorization": f"Bearer {access_token}"}
    response: httpx.Response | None = None
    fault_class: str | None = None
    try:
        async with tai42_app.clients.client_ctx(HttpxClient, timeout=settings.http_timeout_seconds) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        fault_class = type(exc).__name__
    if response is None:
        # An httpx transport fault carries the request URL — and, on its ``.request``, the
        # ``Authorization: Bearer`` header. Re-wrap it OUTSIDE the ``except`` so that
        # credential-bearing exception is neither chained as ``__cause__`` nor left on
        # ``__context__`` of the URL-free fault the caller sees.
        raise MediaFetchError(host=host, cause_class=fault_class)
    if response.status_code != 200:
        raise MediaFetchError(host=host, status_code=response.status_code)
    try:
        body = response.json()
    except ValueError as exc:
        raise MediaFetchError(host=host, status_code=response.status_code) from exc
    if not isinstance(body, dict) or not isinstance(body.get("url"), str) or not body["url"]:
        raise MediaFetchError(host=host, status_code=response.status_code)
    return body


@asynccontextmanager
async def open_media_download(url: str) -> AsyncIterator[MediaStream]:
    """Open a Bearer-authenticated streaming download of an inbound media's lookaside ``url``.

    The Graph lookaside ``url`` is short-lived and serves the bytes to a request carrying the
    access token; redirects are not followed, so a redirect answer is a permanent fetch fault
    rather than a hop the token could ride to another host. Faults surface as
    :class:`~tai42_kit.net.MediaFetchError` (host + status only) or ``UrlGuardError`` from the
    kit — the token rides the request header only, never an error string. The response is closed
    when the context exits.
    """
    settings = whatsapp_settings()
    access_token = require_delivery_secret(settings.access_token, "CHANNEL_WHATSAPP_ACCESS_TOKEN")
    async with open_media_stream(url, headers={"Authorization": f"Bearer {access_token}"}) as stream:
        yield stream
