"""The Flow data endpoint: auth, decrypt, action dispatch, and the react ↔ vendor-wire translation."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.responses import Response
from tai42_kit.settings import reset_all_settings

import tai42_channel_whatsapp.inbound  # noqa: F401  (registers the /flow-data route on the bound stub app)
from tai42_channel_whatsapp.correlation import cache_reaction_form

from .conftest import APP_SECRET, FakeRedis, _StubApp, build_request, compute_signature

_FLOW_PATH = "/flow-data"
_TOKEN = "int-react-1"
_SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": ["g", "s"]},
        "qty": {"type": "integer"},
        "total": {"type": "string"},
    },
    "required": ["tier"],
}
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


@pytest.fixture
def flow_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CHANNEL_WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", _PRIVATE_PEM)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", "https://app.example/api/channels/whatsapp/flow-data")
    monkeypatch.setenv("CHANNEL_WHATSAPP_REDIS_URL", "redis://test/0")
    reset_all_settings()
    yield
    reset_all_settings()


@pytest.fixture
def flow_handler(stub_app: _StubApp):
    route = next(route for route in stub_app.http.routes if route.path == _FLOW_PATH)
    return route.handler


def _encrypt(payload: dict[str, Any]) -> tuple[bytes, bytes, bytes]:
    aes_key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    encrypted_aes_key = _KEY.public_key().encrypt(
        aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    ciphertext = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), None)
    envelope = {
        "encrypted_aes_key": base64.b64encode(encrypted_aes_key).decode(),
        "initial_vector": base64.b64encode(iv).decode(),
        "encrypted_flow_data": base64.b64encode(ciphertext).decode(),
    }
    body = json.dumps(envelope).encode()
    return body, aes_key, iv


def _request(payload: dict[str, Any], *, sign: bool = True, body_override: bytes | None = None):
    body, aes_key, iv = _encrypt(payload)
    if body_override is not None:
        body = body_override
    headers = {"host": "public.example", "content-type": "application/json"}
    if sign:
        headers["x-hub-signature-256"] = compute_signature(body, APP_SECRET)
    return (
        build_request(method="POST", path="/api/channels/whatsapp/flow-data", body=body, headers=headers),
        aes_key,
        iv,
    )


def _decrypt_response(response: Response, aes_key: bytes, iv: bytes) -> dict[str, Any]:
    flipped = bytes(b ^ 0xFF for b in iv)
    ciphertext = base64.b64decode(response.body)
    return json.loads(AESGCM(aes_key).decrypt(flipped, ciphertext, None))


async def _seed(fake_redis: FakeRedis, *, pages: list[dict[str, Any]] | None = None) -> None:
    await cache_reaction_form(_TOKEN, _SCHEMA, pages, {}, {}, datetime.now(UTC) + timedelta(minutes=5))


async def test_ping_returns_the_health_response(flow_env, flow_handler, fake_redis: FakeRedis):
    request, aes_key, iv = _request({"version": "3.0", "action": "ping"})
    response = await flow_handler(request)
    assert response.status_code == 200
    assert _decrypt_response(response, aes_key, iv) == {"data": {"status": "active"}}


async def test_init_action_is_a_benign_no_op(flow_env, flow_handler, fake_redis: FakeRedis):
    request, aes_key, iv = _request({"version": "3.0", "action": "INIT", "screen": "SCREEN_A", "flow_token": _TOKEN})
    response = await flow_handler(request)
    assert _decrypt_response(response, aes_key, iv) == {"screen": "SCREEN_A", "data": {}}


async def test_bad_signature_is_432(flow_env, flow_handler, fake_redis: FakeRedis):
    request, _, _ = _request({"action": "ping"}, sign=False)
    response = await flow_handler(request)
    assert response.status_code == 432


async def test_undecryptable_body_is_421(flow_env, flow_handler, fake_redis: FakeRedis):
    request, _, _ = _request(
        {"action": "ping"}, body_override=b'{"encrypted_aes_key":"xx","initial_vector":"xx","encrypted_flow_data":"xx"}'
    )
    response = await flow_handler(request)
    assert response.status_code == 421


async def test_unknown_flow_token_is_427(flow_env, flow_handler, fake_redis: FakeRedis):
    # No sidecar seeded → the flow token names no open reacting form.
    request, _, _ = _request({"action": "data_exchange", "flow_token": _TOKEN, "screen": "SCREEN_A", "data": {}})
    response = await flow_handler(request)
    assert response.status_code == 427


async def test_field_changed_routes_to_react_and_applies_the_update(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub = stub_app.interactions
    stub.react_result = {"options": {"tier": [{"value": "g", "label": "Gold"}]}, "display": {}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g", "qty": "5"},
    }
    request, aes_key, iv = _request(payload)
    response = await flow_handler(request)

    call = stub.react_calls[-1]
    assert call["interaction_id"] == _TOKEN
    assert call["event"] == {"kind": "field_changed", "field": "tier"}
    # The number field was coerced; the markers were stripped from the partial values.
    assert call["partial_values"] == {"tier": "g", "qty": 5}
    out = _decrypt_response(response, aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert out["data"]["tier__ds"] == [{"id": "g", "title": "Gold"}]


async def test_reaction_option_second_line_rides_the_data_source(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # A reaction that replaces a field's choices mid-form carries each option's second line into
    # the data-source ``description`` exactly as the publish path does.
    await _seed(fake_redis)
    stub = stub_app.interactions
    stub.react_result = {
        "options": {"tier": [{"value": "g", "label": "Gold", "description": "2 hours, 50"}, {"value": "s"}]}
    }
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g", "qty": "5"},
    }
    request, aes_key, iv = _request(payload)
    response = await flow_handler(request)

    out = _decrypt_response(response, aes_key, iv)
    assert out["data"]["tier__ds"] == [
        {"id": "g", "title": "Gold", "description": "2 hours, 50"},
        {"id": "s", "title": "s"},
    ]


async def test_submit_with_no_errors_completes_the_flow(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub = stub_app.interactions
    stub.react_result = {"values": {"total": "42"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "submitted", "tier": "g"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"
    params = out["data"]["extension_message_response"]["params"]
    assert params["flow_token"] == _TOKEN
    # Keyed by the human-readable completion labels; the submit reaction's value is folded in.
    assert params["tier"] == "g"
    assert params["total"] == "42"


async def test_submit_with_errors_keeps_the_form_open(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub = stub_app.interactions
    stub.react_result = {"errors": {"tier": "not available"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "submitted", "tier": "g"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert "tier: not available" in out["data"]["error_message"]


async def test_react_failure_surfaces_the_vendor_error_notice(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub = stub_app.interactions
    stub.react_error = RuntimeError("handler blew up")
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert "could not be updated" in out["data"]["error_message"]


async def test_missing_event_marker_surfaces_the_error_notice(flow_env, flow_handler, fake_redis: FakeRedis):
    await _seed(fake_redis)
    payload = {"action": "data_exchange", "flow_token": _TOKEN, "screen": "SCREEN_A", "data": {"tier": "g"}}
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert "error_message" in out["data"]


async def test_page_advance_moves_to_the_next_screen_with_collected_values(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    pages = [
        {"title": "First", "fields": ["tier", "qty"], "kind": "input", "display": []},
        {"title": "Second", "fields": ["total"], "kind": "input", "display": []},
    ]
    await _seed(fake_redis, pages=pages)
    stub = stub_app.interactions
    stub.react_result = {}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "page_advanced", "tai42_page": "First", "tier": "g", "qty": "5"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    # The collected values ride forward as their __val carriers.
    assert out["data"]["tier__val"] == "g"
    assert out["data"]["qty__val"] == "5"


async def test_missing_app_secret_is_a_loud_500(flow_handler, fake_redis: FakeRedis, no_whatsapp_env):
    request, _, _ = _request({"action": "ping"}, sign=False)
    response = await flow_handler(request)
    assert response.status_code == 500


async def test_registered_public_key_matches_the_private_key():
    # The derived public key the provisioning step registers is the private key's own public key.
    from tai42_channel_whatsapp.flow_crypto import public_key_pem

    expected = (
        _KEY.public_key()
        .public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    assert public_key_pem(_KEY) == expected


async def test_oversize_body_is_413(flow_env, flow_handler, fake_redis: FakeRedis):
    request, _, _ = _request({"action": "ping"}, body_override=b"x" * (1024 * 1024 + 1))
    response = await flow_handler(request)
    assert response.status_code == 413


async def test_unreadable_private_key_is_a_loud_500(
    flow_handler, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CHANNEL_WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", "not a real key")
    monkeypatch.setenv("CHANNEL_WHATSAPP_REDIS_URL", "redis://test/0")
    reset_all_settings()
    try:
        request, _, _ = _request({"action": "ping"})
        response = await flow_handler(request)
        assert response.status_code == 500
    finally:
        reset_all_settings()


async def test_non_object_envelope_is_421(flow_env, flow_handler, fake_redis: FakeRedis):
    request, _, _ = _request({"action": "ping"}, body_override=b"[1, 2, 3]")
    response = await flow_handler(request)
    assert response.status_code == 421


async def test_unexpected_action_is_a_benign_no_op(flow_env, flow_handler, fake_redis: FakeRedis):
    request, aes_key, iv = _request({"action": "BOGUS", "screen": "SCREEN_A"})
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out == {"screen": "SCREEN_A", "data": {}}


async def test_page_advance_on_the_last_screen_stays_put(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)  # single-page form: SCREEN_A is terminal
    stub_app.interactions.react_result = {}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "page_advanced", "tai42_page": "Form", "tier": "g"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"


async def test_display_slot_update_fills_the_slot_data_key(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    pages = [
        {
            "title": "Form",
            "fields": ["tier", "qty", "total"],
            "kind": "input",
            "display": [{"kind": "body", "slot": "summary"}],
        }
    ]
    await _seed(fake_redis, pages=pages)
    stub_app.interactions.react_result = {"display": {"summary": "Total: 42"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["slot_summary"] == "Total: 42"


async def test_missing_private_key_is_a_loud_500(flow_handler, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.delenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("CHANNEL_WHATSAPP_REDIS_URL", "redis://test/0")
    reset_all_settings()
    try:
        request, _, _ = _request({"action": "ping"})
        assert (await flow_handler(request)).status_code == 500
    finally:
        reset_all_settings()


async def test_data_exchange_without_flow_token_is_427(flow_env, flow_handler, fake_redis: FakeRedis):
    request, _, _ = _request({"action": "data_exchange", "screen": "SCREEN_A", "data": {"tai42_event": "submitted"}})
    assert (await flow_handler(request)).status_code == 427


async def test_field_changed_values_update_sets_the_field_init(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub_app.interactions.react_result = {"values": {"qty": 7}, "options": {"tier": [{"value": "g"}]}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["qty__init"] == "7"
    # An option with no label falls back to its value for the title.
    assert out["data"]["tier__ds"] == [{"id": "g", "title": "g"}]


async def test_page_advance_applies_reaction_options_and_defaults_unfilled_val(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    pages = [
        {"title": "First", "fields": ["tier"], "kind": "input", "display": []},
        {"title": "Second", "fields": ["qty", "total"], "kind": "input", "display": []},
    ]
    await _seed(fake_redis, pages=pages)
    stub_app.interactions.react_result = {"options": {"tier": [{"value": "s", "label": "Silver"}]}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "page_advanced", "tai42_page": "First", "tier": "s"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    assert out["data"]["tier__ds"] == [{"id": "s", "title": "Silver"}]


async def test_reaction_error_message_uses_the_field_title_not_the_key(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    schema = {
        "type": "object",
        "properties": {"qty": {"type": "integer", "title": "Quantity"}},
        "required": ["qty"],
    }
    await cache_reaction_form(_TOKEN, schema, None, {}, {}, datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {"errors": {"qty": "must be at least 1"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "submitted", "qty": "0"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    # The guest reads the human label the field renders, not the raw schema key.
    assert out["data"]["error_message"] == "Quantity: must be at least 1"


async def test_reaction_error_message_falls_back_to_the_key_without_a_title(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    schema = {
        "type": "object",
        "properties": {"qty": {"type": "integer"}},
        "required": ["qty"],
    }
    await cache_reaction_form(_TOKEN, schema, None, {}, {}, datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {"errors": {"qty": "must be at least 1"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "submitted", "qty": "0"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    # No title: the field renders under its key, and so does its error.
    assert out["data"]["error_message"] == "qty: must be at least 1"
