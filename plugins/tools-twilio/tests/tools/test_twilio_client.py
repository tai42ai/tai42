"""Tests for the shared Twilio client: credential guards, the Basic-auth header on
the wire, paging to exhaustion and its ceiling, path-segment encoding, non-2xx
propagation, and redirect refusal (the Basic-auth header never reaches a redirect
host).

The Twilio calls run through tai42-kit's real curl client against a loopback stub
(a transport-level mock cannot intercept curl_cffi); no test reaches a Twilio host.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

import pytest

import tai42_tools_twilio._internal.tools.twilio_client as twilio_client
from tai42_tools_twilio._internal.tools.twilio_client import (
    _auth_header,
    get_incoming_phone_number,
    list_incoming_phone_numbers,
    update_incoming_phone_number_sms_url,
)

_JSON = {"Content-Type": "application/json"}

# --- credential guards (raise before any network work) --------------------------------------


@pytest.mark.parametrize("account_sid", [None, ""])
def test_missing_or_empty_account_sid_raises(twilio_env: Callable[..., None], account_sid: str | None) -> None:
    twilio_env(account_sid=account_sid)
    with pytest.raises(ValueError, match="CHANNEL_TWILIO_ACCOUNT_SID"):
        asyncio.run(list_incoming_phone_numbers())


@pytest.mark.parametrize("auth_token", [None, ""])
def test_missing_or_empty_auth_token_raises(twilio_env: Callable[..., None], auth_token: str | None) -> None:
    twilio_env(auth_token=auth_token)
    with pytest.raises(ValueError, match="CHANNEL_TWILIO_AUTH_TOKEN"):
        asyncio.run(list_incoming_phone_numbers())


def test_auth_header_is_basic_of_sid_and_token(twilio_env: Callable[..., None]) -> None:
    twilio_env(account_sid="AC_abc", auth_token="tok_xyz")
    expected = "Basic " + base64.b64encode(b"AC_abc:tok_xyz").decode("ascii")
    assert _auth_header() == expected


# --- Basic-auth header on the wire ----------------------------------------------------------


@pytest.mark.usefixtures("curl_app")
def test_request_sends_basic_auth_header(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(account_sid="AC_abc", auth_token="tok_xyz", api_base_url=stub_server.base_url)
    stub_server.set_responder(
        lambda _r: (200, _JSON, '{"incoming_phone_numbers": [{"sid": "PN1"}], "next_page_uri": null}')
    )
    numbers = asyncio.run(list_incoming_phone_numbers())
    assert [n["sid"] for n in numbers] == ["PN1"]

    request = stub_server.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/Accounts/AC_abc/IncomingPhoneNumbers.json"
    expected = "Basic " + base64.b64encode(b"AC_abc:tok_xyz").decode("ascii")
    assert request["headers"]["authorization"] == expected


# --- paging ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("curl_app")
def test_list_follows_next_page_uri_to_exhaustion(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    calls = {"n": 0}

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                200,
                _JSON,
                '{"incoming_phone_numbers": [{"sid": "PN1"}], '
                '"next_page_uri": "/Accounts/AC_test/IncomingPhoneNumbers.json?Page=1"}',
            )
        return 200, _JSON, '{"incoming_phone_numbers": [{"sid": "PN2"}], "next_page_uri": null}'

    stub_server.set_responder(responder)
    numbers = asyncio.run(list_incoming_phone_numbers())
    assert [n["sid"] for n in numbers] == ["PN1", "PN2"]
    assert stub_server.requests[1]["query"]["Page"] == ["1"]


@pytest.mark.usefixtures("curl_app")
def test_list_raises_at_page_ceiling(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        page = json.dumps(
            {
                "incoming_phone_numbers": [{"sid": "PN"}],
                "next_page_uri": "/Accounts/AC_test/IncomingPhoneNumbers.json?Page=1",
            }
        )
        return 200, _JSON, page

    stub_server.set_responder(responder)
    with pytest.raises(ValueError, match="page ceiling"):
        asyncio.run(list_incoming_phone_numbers())
    assert len([r for r in stub_server.requests if r["method"] == "GET"]) == 100


@pytest.mark.usefixtures("curl_app")
def test_list_raises_on_non_2xx(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(lambda _r: (401, _JSON, '{"message": "Authentication Error"}'))
    with pytest.raises(ValueError, match="list failed: HTTP 401") as excinfo:
        asyncio.run(list_incoming_phone_numbers())
    assert "Authentication Error" in str(excinfo.value)


# --- path-segment encoding and non-2xx on the single-resource calls -------------------------


def test_get_percent_encodes_the_sid_path_segment(
    twilio_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    twilio_env(api_base_url="https://api.twilio.test/2010-04-01")
    captured: dict[str, str] = {}

    async def fake_request(method: str, url: str, **_kwargs: Any) -> tuple[int, dict[str, str], str]:
        captured["method"] = method
        captured["url"] = url
        return 200, {}, '{"sid": "PN1"}'

    monkeypatch.setattr(twilio_client, "_http_request", fake_request)
    asyncio.run(get_incoming_phone_number("PN1/../AC?x=1"))
    assert captured["method"] == "GET"
    assert captured["url"] == (
        "https://api.twilio.test/2010-04-01/Accounts/AC_test/IncomingPhoneNumbers/PN1%2F..%2FAC%3Fx%3D1.json"
    )


@pytest.mark.usefixtures("curl_app")
def test_get_raises_on_non_2xx(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(lambda _r: (404, _JSON, '{"message": "The requested resource was not found"}'))
    with pytest.raises(ValueError, match="read failed: HTTP 404"):
        asyncio.run(get_incoming_phone_number("PN_missing"))


@pytest.mark.usefixtures("curl_app")
def test_update_posts_form_encoded_sms_url(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(lambda _r: (200, _JSON, '{"sid": "PN1", "sms_url": "https://app.example/in"}'))
    asyncio.run(update_incoming_phone_number_sms_url("PN1", "https://app.example/in?a=b&c=d"))

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/Accounts/AC_test/IncomingPhoneNumbers/PN1.json"
    assert request["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert request["body"] == "SmsUrl=https%3A%2F%2Fapp.example%2Fin%3Fa%3Db%26c%3Dd"


@pytest.mark.usefixtures("curl_app")
def test_update_raises_on_non_2xx(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(lambda _r: (400, _JSON, '{"message": "Url is not a valid URL"}'))
    with pytest.raises(ValueError, match="update failed: HTTP 400"):
        asyncio.run(update_incoming_phone_number_sms_url("PN1", "https://app.example/in"))


# --- redirects off: the Basic-auth header never reaches a redirect host ----------------------


@pytest.mark.usefixtures("curl_app")
def test_list_does_not_follow_redirect_and_hides_token(
    twilio_env: Callable[..., None], stub_server: Any, local_server: Any
) -> None:
    twilio_env(account_sid="AC_abc", auth_token="tok_secret", api_base_url=stub_server.base_url)
    target = f"{local_server.base_url}/Accounts/AC_abc/IncomingPhoneNumbers.json"
    stub_server.set_responder(lambda _r: (302, {"Location": target}, ""))

    with pytest.raises(ValueError, match="list failed: HTTP 302") as excinfo:
        asyncio.run(list_incoming_phone_numbers())
    # The redirect target recorded no request: the Authorization header never left the pinned host.
    assert local_server.requests == []
    message = str(excinfo.value)
    assert "tok_secret" not in message
    assert "Basic" not in message
