"""Tests for the ``subscribe_whatsapp_app`` tool: the plain subscribe with no body, the override
pair when both params are given, the one-of-two raise both ways, and non-2xx propagation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_whatsapp.tools.subscribe_whatsapp_app import subscribe_whatsapp_app


def _responder(*, code: int = 200, body: str = '{"success": true}') -> Callable[..., Any]:
    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return code, {"Content-Type": "application/json"}, body

    return responder


@pytest.mark.usefixtures("curl_app")
def test_plain_subscribe_sends_no_body(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = asyncio.run(subscribe_whatsapp_app())
    assert result == {"success": True}

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/WABA_1/subscribed_apps"
    # No override params -> no JSON override body on the wire.
    assert request["body"] == ""


@pytest.mark.usefixtures("curl_app")
def test_override_pair_sends_both_fields(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder())

    asyncio.run(subscribe_whatsapp_app(callback_uri="https://acme.example/hook", verify_token="vt-1"))
    request = stub_server.requests[0]
    assert request["headers"]["content-type"] == "application/json"
    body = json.loads(request["body"])
    assert body == {"override_callback_uri": "https://acme.example/hook", "verify_token": "vt-1"}


@pytest.mark.parametrize(
    ("callback_uri", "verify_token"),
    [("https://acme.example/hook", None), (None, "vt-1")],
)
def test_exactly_one_param_raises(callback_uri: str | None, verify_token: str | None) -> None:
    with pytest.raises(ValueError, match="together or both omitted"):
        asyncio.run(subscribe_whatsapp_app(callback_uri=callback_uri, verify_token=verify_token))


@pytest.mark.parametrize("callback_uri", ["http://acme.example/hook", "https://", "ftp://acme.example/hook"])
def test_non_https_callback_uri_rejected(callback_uri: str) -> None:
    # Validated before any network work; a verify_token is paired so only the URL check can fire.
    with pytest.raises(ValueError, match="callback_uri must be an https URL with a host"):
        asyncio.run(subscribe_whatsapp_app(callback_uri=callback_uri, verify_token="vt-1"))


@pytest.mark.usefixtures("curl_app")
def test_https_callback_uri_accepted(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = asyncio.run(subscribe_whatsapp_app(callback_uri="https://acme.example/hook", verify_token="vt-1"))
    assert result == {"success": True}


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder(code=403, body='{"error": {"message": "not permitted"}}'))
    with pytest.raises(ValueError, match="subscribe failed") as excinfo:
        asyncio.run(subscribe_whatsapp_app())
    assert "403" in str(excinfo.value)
    assert "not permitted" in str(excinfo.value)


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_whatsapp.tools.subscribe_whatsapp_app")
    assert set(app.tools.registered) == {"subscribe_whatsapp_app"}
