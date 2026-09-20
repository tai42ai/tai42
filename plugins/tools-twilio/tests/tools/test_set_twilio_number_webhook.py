"""Tests for the ``set_twilio_number_webhook`` tool: the form-encoded POST, the
projected fields, the empty/non-https ``sms_url`` guards, the empty
``phone_number_sid`` guard, and registration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_twilio.tools.set_twilio_number_webhook import set_twilio_number_webhook

_JSON = {"Content-Type": "application/json"}


@pytest.mark.usefixtures("curl_app")
def test_happy_path_posts_sms_url_and_projects(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(
        lambda _r: (
            200,
            _JSON,
            '{"sid": "PN1", "phone_number": "+15551230000", "sms_url": "https://app.example/in"}',
        )
    )
    result = asyncio.run(set_twilio_number_webhook("PN1", "https://app.example/in"))

    assert result == {"sid": "PN1", "phone_number": "+15551230000", "sms_url": "https://app.example/in"}
    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/Accounts/AC_test/IncomingPhoneNumbers/PN1.json"
    assert request["body"] == "SmsUrl=https%3A%2F%2Fapp.example%2Fin"


def test_empty_sid_raises() -> None:
    with pytest.raises(ValueError, match="phone_number_sid"):
        asyncio.run(set_twilio_number_webhook("", "https://app.example/in"))


def test_empty_sms_url_raises() -> None:
    with pytest.raises(ValueError, match="sms_url is required"):
        asyncio.run(set_twilio_number_webhook("PN1", ""))


@pytest.mark.parametrize(
    "sms_url",
    ["http://app.example/in", "ftp://app.example/in", "app.example/in", "https://"],
)
def test_non_https_sms_url_raises(sms_url: str) -> None:
    with pytest.raises(ValueError, match="sms_url must be an https URL with a host"):
        asyncio.run(set_twilio_number_webhook("PN1", sms_url))


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_twilio.tools.set_twilio_number_webhook")
    assert set(app.tools.registered) == {"set_twilio_number_webhook"}
