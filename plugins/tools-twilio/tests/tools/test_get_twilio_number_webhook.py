"""Tests for the ``get_twilio_number_webhook`` tool: the projected fields, the empty
``phone_number_sid`` guard, and registration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_twilio.tools.get_twilio_number_webhook import get_twilio_number_webhook

_JSON = {"Content-Type": "application/json"}


@pytest.mark.usefixtures("curl_app")
def test_happy_path_projects_the_webhook(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(
        lambda _r: (
            200,
            _JSON,
            '{"sid": "PN1", "phone_number": "+15551230000", "sms_url": "https://app.example/in", '
            '"sms_method": "POST", "voice_url": "ignored"}',
        )
    )
    result = asyncio.run(get_twilio_number_webhook("PN1"))
    assert result == {
        "sid": "PN1",
        "phone_number": "+15551230000",
        "sms_url": "https://app.example/in",
        "sms_method": "POST",
    }
    assert stub_server.requests[0]["method"] == "GET"
    assert stub_server.requests[0]["path"] == "/Accounts/AC_test/IncomingPhoneNumbers/PN1.json"


def test_empty_sid_raises() -> None:
    with pytest.raises(ValueError, match="phone_number_sid"):
        asyncio.run(get_twilio_number_webhook(""))


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_twilio.tools.get_twilio_number_webhook")
    assert set(app.tools.registered) == {"get_twilio_number_webhook"}
