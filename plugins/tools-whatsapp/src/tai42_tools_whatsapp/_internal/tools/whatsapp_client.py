"""Direct Meta Graph client for the WhatsApp provisioning tools.

Speaks the Graph API through tai42-kit's curl client (no vendor SDK): registers, lists and deletes
WhatsApp message templates on a WhatsApp Business Account and subscribes the app to that account's
webhooks. The Graph host and pinned API version ride ``CHANNEL_WHATSAPP_API_BASE_URL`` (default
``https://graph.facebook.com/v23.0``); credentials ride ``CHANNEL_WHATSAPP_ACCESS_TOKEN`` and
``CHANNEL_WHATSAPP_WABA_ID``.

Every non-2xx raises ``ValueError`` loudly carrying Graph's status and body, never caught or
remapped, and no request-header content — the Bearer token above all — is ever echoed into the
raised text. Any caller value placed into a URL path or query is percent-encoded.
"""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.curl import CurlClient
from tai42_kit.settings import TaiBaseSettings, settings_cache

# Hard page ceiling on the template-list cursor follow: Graph pages with a ``paging.next`` URL, and
# a self-referential or runaway cursor would loop forever. RAISES at this bound rather than
# truncating silently.
_LIST_PAGE_CEILING = 100


class WhatsAppToolsSettings(TaiBaseSettings):
    """Provisioning-tool configuration, read from the ``CHANNEL_WHATSAPP_`` env group shared with
    the rest of the WhatsApp deployment. The access token is ``SecretStr`` (never in a
    repr/log/traceback); its plaintext is read only at the Bearer-auth seam."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_WHATSAPP_")

    # Graph API bearer token (one token serves the business account's numbers).
    access_token: SecretStr | None = None
    # WhatsApp Business Account id the template and subscribe endpoints hang under.
    waba_id: str | None = None
    # Graph API origin with a pinned version; overridable so a stub can stand in (e2e).
    api_base_url: str = "https://graph.facebook.com/v23.0"
    # Per-request HTTP timeout in seconds (same name/default the channel plugin uses).
    http_timeout_seconds: float = Field(default=30.0, gt=0)


@settings_cache
def whatsapp_settings() -> WhatsAppToolsSettings:
    return WhatsAppToolsSettings()


def _access_token() -> str:
    """The configured Graph access token, revealed. Missing and EMPTY are the same failure and both
    raise naming ``CHANNEL_WHATSAPP_ACCESS_TOKEN``: an empty env var parses to ``SecretStr("")``
    (not ``None``), so a plain ``is None`` test would send ``Authorization: Bearer`` to Graph."""
    token = whatsapp_settings().access_token
    if not (token and token.get_secret_value()):
        raise ValueError("CHANNEL_WHATSAPP_ACCESS_TOKEN is not set (missing or empty)")
    return token.get_secret_value()


def _waba_id() -> str:
    """The configured WhatsApp Business Account id. Missing and empty both raise naming
    ``CHANNEL_WHATSAPP_WABA_ID``; the value is path-interpolated only after passing this gate."""
    waba_id = whatsapp_settings().waba_id
    if not waba_id:
        raise ValueError("CHANNEL_WHATSAPP_WABA_ID is not set (missing or empty)")
    return waba_id


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_access_token()}"}


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: str | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    """Issue one request through a fresh curl session and return ``(status, lowercased headers,
    body text)``. Redirects are OFF: every Graph call carries ``Authorization: Bearer <token>`` and
    libcurl replays custom headers across a redirect hop — a 302 off Graph would hand that header to
    another host."""
    session_ctx = tai42_app.clients.client_ctx(CurlClient, session_params={}, fresh=True)
    async with session_ctx as session:
        resp = await session.request(
            url=url,
            method=cast("Any", method),
            headers=headers,
            data=data,
            params=params,
            timeout=whatsapp_settings().http_timeout_seconds,
            allow_redirects=False,
        )
        resp_headers = {key.lower(): value for key, value in resp.headers.items() if value is not None}
        return resp.status_code, resp_headers, resp.text


def _templates_url() -> str:
    return f"{whatsapp_settings().api_base_url}/{quote(_waba_id(), safe='')}/message_templates"


async def register_template(name: str, language: str, category: str, components: list[Any]) -> dict[str, Any]:
    """POST ``/{waba_id}/message_templates`` with a JSON body and return the response JSON whole.

    ``components`` is the Graph-shaped component list passed through unchanged. A non-2xx raises
    loudly with Graph's status and body; no request-header content is echoed into the raised text.
    """
    body = json.dumps({"name": name, "language": language, "category": category, "components": components})
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    status, _resp_headers, text = await _http_request("POST", _templates_url(), headers=headers, data=body)
    if not 200 <= status < 300:
        raise ValueError(f"WhatsApp template register failed: HTTP {status} {text}")
    return json.loads(text)


async def list_templates() -> list[dict[str, Any]]:
    """GET ``/{waba_id}/message_templates`` and follow Graph's ``paging.next`` cursor across pages.

    Each page's ``data`` accumulates; the loop ends when Graph stops sending a ``paging.next`` URL,
    and RAISES a named-ceiling error at ``_LIST_PAGE_CEILING`` pages rather than following a cursor
    forever. A non-2xx on any page raises loudly with Graph's status and body.
    """
    url: str | None = _templates_url()
    headers = _auth_headers()
    templates: list[dict[str, Any]] = []
    pages = 0
    while url is not None:
        if pages >= _LIST_PAGE_CEILING:
            raise ValueError(f"WhatsApp message template list exceeded its {_LIST_PAGE_CEILING}-page ceiling")
        status, _resp_headers, text = await _http_request("GET", url, headers=headers)
        if not 200 <= status < 300:
            raise ValueError(f"WhatsApp template list failed: HTTP {status} {text}")
        payload = json.loads(text)
        pages += 1
        templates.extend(payload.get("data") or [])
        url = (payload.get("paging") or {}).get("next")
    return templates


async def delete_template(name: str) -> dict[str, Any]:
    """DELETE ``/{waba_id}/message_templates?name=...`` and return the response JSON whole.

    ``name`` is the sole query-interpolated caller value; it rides ``params`` so the curl client
    percent-encodes it and it cannot inject further query parameters. A non-2xx raises loudly with
    Graph's status and body.
    """
    status, _resp_headers, text = await _http_request(
        "DELETE", _templates_url(), headers=_auth_headers(), params={"name": name}
    )
    if not 200 <= status < 300:
        raise ValueError(f"WhatsApp template delete failed: HTTP {status} {text}")
    return json.loads(text)


async def subscribe_app(callback_uri: str | None, verify_token: str | None) -> dict[str, Any]:
    """POST ``/{waba_id}/subscribed_apps`` and return the response JSON whole.

    With both ``callback_uri`` and ``verify_token`` supplied, sends Meta's
    ``override_callback_uri`` + ``verify_token`` override pair; with neither, subscribes to the
    app's configured webhook. Supplying exactly one of the two is a caller error and raises. A
    non-2xx raises loudly with Graph's status and body.
    """
    if (callback_uri is None) != (verify_token is None):
        raise ValueError("callback_uri and verify_token must be given together or both omitted")
    url = f"{whatsapp_settings().api_base_url}/{quote(_waba_id(), safe='')}/subscribed_apps"
    headers = _auth_headers()
    data: str | None = None
    if callback_uri is not None and verify_token is not None:
        headers = {**headers, "Content-Type": "application/json"}
        data = json.dumps({"override_callback_uri": callback_uri, "verify_token": verify_token})
    status, _resp_headers, text = await _http_request("POST", url, headers=headers, data=data)
    if not 200 <= status < 300:
        raise ValueError(f"WhatsApp app subscribe failed: HTTP {status} {text}")
    return json.loads(text)
