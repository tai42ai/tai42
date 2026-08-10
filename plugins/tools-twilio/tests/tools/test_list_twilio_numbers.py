"""Tests for the ``list_twilio_numbers`` tool: the projected fields, paging carried
through from the client, and registration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_twilio.tools.list_twilio_numbers import list_twilio_numbers

_JSON = {"Content-Type": "application/json"}


@pytest.mark.usefixtures("curl_app")
def test_happy_path_projects_and_pages(twilio_env: Callable[..., None], stub_server: Any) -> None:
    twilio_env(api_base_url=stub_server.base_url)
    calls = {"n": 0}

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                200,
                _JSON,
                '{"incoming_phone_numbers": [{"sid": "PN1", "phone_number": "+15551230000", '
                '"sms_url": "https://app.example/in", "sms_method": "POST", "voice_url": "ignored"}], '
                '"next_page_uri": "/Accounts/AC_test/IncomingPhoneNumbers.json?Page=1"}',
            )
        return (
            200,
            _JSON,
            '{"incoming_phone_numbers": [{"sid": "PN2", "phone_number": "+15551230001", '
            '"sms_url": "", "sms_method": "GET"}], "next_page_uri": null}',
        )

    stub_server.set_responder(responder)
    result = asyncio.run(list_twilio_numbers())
    assert result == [
        {"sid": "PN1", "phone_number": "+15551230000", "sms_url": "https://app.example/in", "sms_method": "POST"},
        {"sid": "PN2", "phone_number": "+15551230001", "sms_url": "", "sms_method": "GET"},
    ]


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_twilio.tools.list_twilio_numbers")
    assert set(app.tools.registered) == {"list_twilio_numbers"}
