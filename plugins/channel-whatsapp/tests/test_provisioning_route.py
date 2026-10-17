"""The admin door that registers the Flow encryption public key: body parsing, the loud
configuration and Graph refusals, and the private key never leaving the server."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.responses import Response
from tai42_kit.settings import reset_all_settings

import tai42_channel_whatsapp.provisioning_route  # noqa: F401  (registers /flow-encryption on the bound stub app)
from tai42_channel_whatsapp.flow_crypto import public_key_pem

from .conftest import PHONE_NUMBER_ID, FakeHttpx, _StubApp, build_request, response

_PATH = "/flow-encryption"
_SERVED_PATH = "/api/channels/whatsapp/flow-encryption"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
# The base64 body lines of the private PEM: any one of them in a reply or a log line is a leak.
_PRIVATE_LINES = [line for line in _PRIVATE_PEM.splitlines() if line and not line.startswith("-----")]


@pytest.fixture
def provision_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CHANNEL_WHATSAPP_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID", PHONE_NUMBER_ID)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _PRIVATE_PEM)
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY_PASSPHRASE", raising=False)
    reset_all_settings()
    yield
    reset_all_settings()


@pytest.fixture
def route_handler(stub_app: _StubApp) -> Callable[[Any], Awaitable[Response]]:
    (route,) = [route for route in stub_app.http.routes if route.path == _PATH]
    return route.handler


async def _call(handler: Callable[[Any], Awaitable[Response]], body: bytes) -> tuple[int, dict[str, Any]]:
    headers = {"host": "app.example", "content-type": "application/json"}
    reply = await handler(build_request(method="POST", path=_SERVED_PATH, body=body, headers=headers))
    text = bytes(reply.body).decode()
    assert not any(line in text for line in _PRIVATE_LINES), "the private key must never leave the server"
    return reply.status_code, json.loads(text)


def _accept(fake_httpx: FakeHttpx) -> None:
    fake_httpx.responses.append(response(200, json={"success": True}))


async def test_empty_body_registers_the_default_number(provision_env, fake_httpx: FakeHttpx, route_handler):
    _accept(fake_httpx)

    status, body = await _call(route_handler, b"")

    assert status == 200
    (call,) = fake_httpx.calls
    assert call["url"].endswith(f"/{PHONE_NUMBER_ID}/whatsapp_business_encryption")
    assert call["json"]["business_public_key"] == public_key_pem(_KEY)
    assert body == {"data": {"phone_number_id": PHONE_NUMBER_ID, "public_key": public_key_pem(_KEY)}}


async def test_body_number_targets_that_number(provision_env, fake_httpx: FakeHttpx, route_handler):
    _accept(fake_httpx)

    status, body = await _call(route_handler, json.dumps({"phone_number_id": "99999"}).encode())

    assert status == 200
    (call,) = fake_httpx.calls
    assert call["url"].endswith("/99999/whatsapp_business_encryption")
    assert body["data"]["phone_number_id"] == "99999"


async def test_empty_object_registers_the_default_number(provision_env, fake_httpx: FakeHttpx, route_handler):
    _accept(fake_httpx)

    status, body = await _call(route_handler, b"{}")

    assert status == 200
    (call,) = fake_httpx.calls
    assert call["url"].endswith(f"/{PHONE_NUMBER_ID}/whatsapp_business_encryption")
    assert body == {"data": {"phone_number_id": PHONE_NUMBER_ID, "public_key": public_key_pem(_KEY)}}


async def test_invalid_json_is_400_without_a_graph_call(provision_env, fake_httpx: FakeHttpx, route_handler):
    status, body = await _call(route_handler, b"{not json")

    assert status == 400
    assert body == {"error": "invalid JSON body"}
    assert fake_httpx.calls == []


async def test_unknown_field_is_422_without_a_graph_call(provision_env, fake_httpx: FakeHttpx, route_handler):
    status, body = await _call(route_handler, json.dumps({"phone": "1"}).encode())

    assert status == 422
    assert body["error"].startswith("invalid request body: phone: ")
    assert fake_httpx.calls == []


async def test_unset_private_key_is_501_naming_the_setting(
    provision_env, monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx, route_handler
):
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY")
    reset_all_settings()

    status, body = await _call(route_handler, b"{}")

    assert status == 501
    assert "CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY" in body["error"]
    assert fake_httpx.calls == []


async def test_unset_default_number_is_501_unless_the_body_names_one(
    provision_env, monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx, route_handler
):
    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()

    status, body = await _call(route_handler, b"{}")

    assert status == 501
    assert "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID" in body["error"]
    assert fake_httpx.calls == []

    _accept(fake_httpx)
    status, body = await _call(route_handler, json.dumps({"phone_number_id": "99999"}).encode())

    assert status == 200
    assert body["data"]["phone_number_id"] == "99999"


async def test_unreadable_private_key_is_501(
    provision_env, monkeypatch: pytest.MonkeyPatch, fake_httpx: FakeHttpx, route_handler
):
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", "not a pem")
    reset_all_settings()

    status, body = await _call(route_handler, b"{}")

    assert status == 501
    assert body == {"error": "WhatsApp flow private key is not a readable PEM private key"}
    assert fake_httpx.calls == []


async def test_graph_refusal_is_502_carrying_the_vendor_error(provision_env, fake_httpx: FakeHttpx, route_handler):
    fake_httpx.responses.append(
        response(400, json={"error": {"message": "(#100) Invalid parameter", "type": "OAuthException", "code": 100}})
    )

    status, body = await _call(route_handler, b"{}")

    assert status == 502
    assert body["error"].startswith("WhatsApp rejected the send: HTTP 400")
    assert "(#100) Invalid parameter" in body["error"]


async def test_transport_failure_is_502(provision_env, fake_httpx: FakeHttpx, route_handler):
    fake_httpx.responses.append(httpx.ConnectError("connection refused"))

    status, body = await _call(route_handler, b"{}")

    assert status == 502
    assert body["error"].startswith("WhatsApp send failed in transport")


async def test_success_logs_the_number_and_never_the_private_key(
    provision_env, fake_httpx: FakeHttpx, route_handler, caplog: pytest.LogCaptureFixture
):
    _accept(fake_httpx)

    with caplog.at_level(logging.DEBUG):
        status, _ = await _call(route_handler, b"{}")

    assert status == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert PHONE_NUMBER_ID in logged
    assert not any(line in logged for line in _PRIVATE_LINES)
