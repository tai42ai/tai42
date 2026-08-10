"""Direct Twilio REST client for the number-provisioning tools.

Speaks Twilio's 2010-04-01 REST API through tai42-kit's curl client with HTTP
Basic auth (``AccountSid:AuthToken``): lists an account's IncomingPhoneNumbers to
paging exhaustion, reads one number resource, and updates a number's ``SmsUrl``.

Every non-2xx raises ``ValueError`` loudly with the status and body and is never
caught or remapped. The ``Authorization`` header is never echoed into a raise, and
redirects are OFF so libcurl cannot replay the Basic-auth header onto a redirect
host.

Credentials come from the ``CHANNEL_TWILIO_`` env group shared with the
deployment's Twilio channel; a missing or empty required credential raises naming
its env var.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast
from urllib.parse import quote, urlencode, urljoin

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.curl import CurlClient
from tai42_kit.settings import TaiBaseSettings, settings_cache

# Hard ceiling on the paging loop: an unbounded follow of ``next_page_uri`` is not
# a bound at all, so a looping or hostile cursor would spin forever. Twilio's
# default page size is 50, so this covers 5000 numbers before raising.
_LIST_PAGE_CEILING = 100


class TwilioSettings(TaiBaseSettings):
    """Twilio provisioning-tool configuration, read from the ``CHANNEL_TWILIO_``
    env group shared with the deployment's Twilio channel: the same account SID,
    auth token, and REST base configure both. The auth token is a ``SecretStr``
    (never in a repr/log/traceback); its plaintext is read only at the Basic-auth
    seam."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_TWILIO_")

    account_sid: str | None = None
    auth_token: SecretStr | None = None
    # Twilio REST origin; the 2010-04-01 version prefix is part of the base, so a
    # relative ``next_page_uri`` resolves against it. Overridable so a stub can
    # stand in; production never changes it.
    api_base_url: str = "https://api.twilio.com/2010-04-01"
    http_timeout_seconds: float = Field(default=30.0, gt=0)


@settings_cache
def twilio_settings() -> TwilioSettings:
    return TwilioSettings()


def _account_sid() -> str:
    """The configured account SID. Missing and EMPTY are the same failure and both
    raise naming ``CHANNEL_TWILIO_ACCOUNT_SID``."""
    sid = twilio_settings().account_sid
    if not sid:
        raise ValueError("CHANNEL_TWILIO_ACCOUNT_SID is not set (missing or empty)")
    return sid


def _auth_token() -> str:
    """The configured auth token, revealed. Missing and EMPTY are the same failure
    and both raise naming ``CHANNEL_TWILIO_AUTH_TOKEN`` (never the value): an empty
    token parses to ``SecretStr("")`` (not ``None``), so a plain ``is None`` test
    would send an empty-password Basic header to Twilio."""
    token = twilio_settings().auth_token
    if not (token and token.get_secret_value()):
        raise ValueError("CHANNEL_TWILIO_AUTH_TOKEN is not set (missing or empty)")
    return token.get_secret_value()


def _auth_header() -> str:
    """The HTTP Basic ``Authorization`` header value for ``AccountSid:AuthToken``."""
    raw = f"{_account_sid()}:{_auth_token()}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: str | None = None,
) -> tuple[int, dict[str, str], str]:
    """Issue one request through a fresh curl session and return ``(status,
    lowercased headers, body text)``. Redirects are OFF: the request carries
    ``Authorization: Basic <token>``, and libcurl replays custom headers across a
    redirect hop -- a 302 off Twilio would hand the Basic-auth header to another
    host.
    """
    session_ctx = tai42_app.clients.client_ctx(CurlClient, session_params={}, fresh=True)
    async with session_ctx as session:
        resp = await session.request(
            url=url,
            method=cast("Any", method),
            headers=headers,
            data=data,
            timeout=twilio_settings().http_timeout_seconds,
            allow_redirects=False,
        )
        resp_headers = {key.lower(): value for key, value in resp.headers.items() if value is not None}
        return resp.status_code, resp_headers, resp.text


def _numbers_collection_url() -> str:
    return f"{twilio_settings().api_base_url}/Accounts/{_account_sid()}/IncomingPhoneNumbers.json"


def _number_resource_url(phone_number_sid: str) -> str:
    # ``phone_number_sid`` is caller input; percent-encode the whole segment
    # (``safe=""`` encodes ``/ ? #``) so it cannot escape into a different path.
    base = f"{twilio_settings().api_base_url}/Accounts/{_account_sid()}/IncomingPhoneNumbers"
    return f"{base}/{quote(phone_number_sid, safe='')}.json"


async def list_incoming_phone_numbers() -> list[dict[str, Any]]:
    """GET every IncomingPhoneNumber resource for the account, following Twilio's
    ``next_page_uri`` cursor to exhaustion. A non-2xx raises loudly, and the loop
    RAISES at ``_LIST_PAGE_CEILING`` pages rather than following a cursor forever."""
    headers = {"Authorization": _auth_header()}
    url = _numbers_collection_url()
    numbers: list[dict[str, Any]] = []
    pages = 0
    while True:
        if pages >= _LIST_PAGE_CEILING:
            raise ValueError(
                f"Twilio incoming phone number list exceeded its {_LIST_PAGE_CEILING}-page ceiling "
                f"({_LIST_PAGE_CEILING * 50} numbers)"
            )
        status, _resp_headers, text = await _http_request("GET", url, headers=headers)
        if not 200 <= status < 300:
            raise ValueError(f"Twilio incoming phone number list failed: HTTP {status} {text}")
        payload = json.loads(text)
        pages += 1
        numbers.extend(payload.get("incoming_phone_numbers") or [])
        next_uri = payload.get("next_page_uri")
        if not next_uri:
            break
        # ``next_page_uri`` is host-absolute (``/2010-04-01/...``); resolve it
        # against the current URL to carry the scheme and host forward.
        url = urljoin(url, next_uri)
    return numbers


async def get_incoming_phone_number(phone_number_sid: str) -> dict[str, Any]:
    """GET one IncomingPhoneNumber resource and return its JSON whole. A non-2xx
    raises loudly with Twilio's status and body."""
    headers = {"Authorization": _auth_header()}
    status, _resp_headers, text = await _http_request("GET", _number_resource_url(phone_number_sid), headers=headers)
    if not 200 <= status < 300:
        raise ValueError(f"Twilio incoming phone number read failed: HTTP {status} {text}")
    return json.loads(text)


async def update_incoming_phone_number_sms_url(phone_number_sid: str, sms_url: str) -> dict[str, Any]:
    """POST a new ``SmsUrl`` to one IncomingPhoneNumber resource (Twilio's
    form-encoded update convention) and return its JSON whole. A non-2xx raises
    loudly with Twilio's status and body; no header content is ever echoed."""
    headers = {
        "Authorization": _auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = urlencode([("SmsUrl", sms_url)])
    status, _resp_headers, text = await _http_request(
        "POST", _number_resource_url(phone_number_sid), headers=headers, data=data
    )
    if not 200 <= status < 300:
        raise ValueError(f"Twilio incoming phone number update failed: HTTP {status} {text}")
    return json.loads(text)
