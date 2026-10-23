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
from tai42_contract.interactions import (
    AnswerFormat,
    FieldChangedEvent,
    FormReactionHandlerError,
    FormShapeError,
    FormShapeKind,
    PageAdvancedEvent,
    SubmittedEvent,
)
from tai42_kit.settings import reset_all_settings

import tai42_channel_whatsapp.inbound  # noqa: F401  (registers the /flow-data route on the bound stub app)
from tai42_channel_whatsapp.correlation import EndpointForm, cache_endpoint_form, cache_flow_form
from tai42_channel_whatsapp.flows import build_form_flow
from tai42_channel_whatsapp.flows_shapes import channel_word
from tai42_channel_whatsapp.inbound.flow_data import _partial_values, _screen_fields

from .conftest import (
    APP_SECRET,
    FakeRedis,
    _StubApp,
    build_request,
    compute_signature,
    phone_footer_payload,
    phone_payload,
)


def test_partial_values_unwraps_a_single_pick_chips_string():
    # A single-pick chips string's partial value arrives as a one-element list and is unwrapped to
    # the string the react facet type-checks; an unfilled (empty-list) chips is omitted.
    props = {"pick": {"type": "string", "enum": ["a", "b", "c"], "control": "chips"}}
    assert _partial_values({"pick": ["b"], "tai42_event": "field_changed"}, props) == {"pick": "b"}
    assert _partial_values({"pick": []}, props) == {}


def test_partial_values_splits_a_merged_range_object_onto_the_two_date_keys():
    props = {
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start"},
    }
    data = {"start": {"start-date": "2026-05-01", "end-date": "2026-05-04"}, "tai42_event": "submitted"}
    assert _partial_values(data, props) == {"start": "2026-05-01", "end": "2026-05-04"}


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


def _reacting_form(schema: dict[str, Any], pages: list[dict[str, Any]] | None) -> EndpointForm:
    # A reacting form declaring every trigger these legs fire: each field's change, each page's
    # advance (the page-less form's one page is "Form") and the submit.
    titles = [page["title"] for page in pages] if pages else ["Form"]
    reactions = {
        "field_changed": list(schema["properties"]),
        "page_advanced": titles,
        "submitted": True,
        "choices": [],
    }
    return EndpointForm(
        schema=schema, pages=pages, reactions=reactions, option_fields=[], invalid={}, required={}, lines={}
    )


async def _seed(fake_redis: FakeRedis, *, pages: list[dict[str, Any]] | None = None) -> None:
    await cache_endpoint_form(_TOKEN, _reacting_form(_SCHEMA, pages), datetime.now(UTC) + timedelta(minutes=5))


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
    assert call["event"] == FieldChangedEvent(field="tier")
    # The number field was coerced; the markers were stripped from the partial values.
    assert call["partial_values"] == {"tier": "g", "qty": 5}
    out = _decrypt_response(response, aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert out["data"]["tier__ds"] == [{"id": "g", "title": "Gold"}]


async def test_a_field_changed_answer_is_the_screens_whole_data_with_the_update_applied(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub_app.interactions.react_result = {"options": {"tier": [{"value": "g", "label": "Gold"}]}}
    screen_data = {
        "tier__init": "",
        "tier__ds": [{"id": "g", "title": "g"}, {"id": "s", "title": "s"}],
        "qty__init": "",
        "qty__err": "",
        "total__init": "preset",
        "total__err": "",
    }
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {
            "tai42_event": "field_changed",
            "tai42_field": "tier",
            "tier": "g",
            "qty": "5",
            "total": "typed",
            **screen_data,
        },
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out == {
        "screen": "SCREEN_A",
        "data": {
            **screen_data,
            "tier__init": "g",
            "tier__ds": [{"id": "g", "title": "Gold"}],
            "qty__init": "5",
            "total__init": "typed",
        },
    }


async def test_a_failed_reaction_keeps_the_screens_whole_data(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed(fake_redis)
    stub_app.interactions.react_error = RuntimeError("handler blew up")
    screen_data = {"tier__init": "", "tier__ds": [{"id": "g", "title": "g"}], "qty__init": "", "total__init": ""}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g", **screen_data},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"] == {
        **screen_data,
        "tier__init": "g",
        "error_message": "Sorry, this form could not be updated right now. Please try again.",
    }


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


async def test_reaction_option_second_line_folds_onto_a_chip_title(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # A reaction that replaces a chips field's choices folds each option's second line onto the chip
    # title (a ChipsSelector item has no description slot) — the row carries no ``description``.
    schema = {
        "type": "object",
        "properties": {
            "tier": {"type": "string", "enum": ["g", "s", "b"], "control": "chips"},
            "qty": {"type": "integer"},
        },
    }
    await cache_endpoint_form(_TOKEN, _reacting_form(schema, None), datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {
        "options": {"tier": [{"value": "g", "label": "Gold", "description": "2 hours, 50"}, {"value": "s"}]}
    }
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "qty", "qty": "5"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["tier__ds"] == [{"id": "g", "title": "Gold \u2014 2 hours, 50"}, {"id": "s", "title": "s"}]


async def test_reaction_option_description_over_cap_surfaces_the_vendor_error(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, caplog
):
    # A reaction whose replaced option carries a second line past Meta's 300-character cap cannot be
    # rendered: the open screen gets the vendor error surface and the failure is logged — never a 5xx.
    await _seed(fake_redis)
    stub_app.interactions.react_result = {
        "options": {"tier": [{"value": "g", "label": "Gold", "description": "d" * 301}]}
    }
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "tier", "tier": "g", "qty": "5"},
    }
    request, aes_key, iv = _request(payload)
    response = await flow_handler(request)
    assert response.status_code == 200
    out = _decrypt_response(response, aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert "could not be updated" in out["data"]["error_message"]
    assert any(record.exc_info and _TOKEN in record.getMessage() for record in caplog.records)


async def test_reaction_chips_option_list_past_meta_max_surfaces_the_vendor_error(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, caplog
):
    # A reaction that replaces a chips field's choices with 21 options cannot be drawn by a
    # ChipsSelector: the open screen gets the vendor error surface and the failure is logged.
    await cache_endpoint_form(_TOKEN, _reacting_form(_CHIPS_SCHEMA, None), datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {"options": {"pick": [{"value": f"v{n}"} for n in range(21)]}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "note", "note": "hi"},
    }
    request, aes_key, iv = _request(payload)
    response = await flow_handler(request)
    assert response.status_code == 200
    out = _decrypt_response(response, aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert "could not be updated" in out["data"]["error_message"]
    assert "pick__ds" not in out["data"]
    assert any(record.exc_info and _TOKEN in record.getMessage() for record in caplog.records)


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


async def test_page_advance_answers_the_next_screens_display_slots(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # The next screen's data is whole: a display slot no reaction filled reads empty.
    pages = [
        {"title": "First", "fields": ["tier", "qty"], "kind": "input", "display": [{"kind": "body", "slot": "a"}]},
        {"title": "Second", "fields": ["total"], "kind": "input", "display": [{"kind": "body", "slot": "b"}]},
    ]
    await _seed(fake_redis, pages=pages)
    stub_app.interactions.react_result = {}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "page_advanced", "tai42_page": "First", "tier": "g", "qty": "5", "slot_a": "x"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["slot_b"] == ""
    assert "slot_a" not in out["data"]


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


_CHIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "pick": {"type": "string", "enum": ["a", "b", "c"], "control": "chips"},
        "note": {"type": "string"},
    },
    "required": ["pick"],
}


async def test_page_advance_carries_a_single_pick_chips_val_as_a_one_element_list(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # A single-pick chips field's data carrier is ARRAY-typed (``max-selected-items: 1``), so its
    # forwarded ``__val`` on a page advance must be a one-element LIST, not a scalar string — a scalar
    # would mismatch the published ChipsSelector's array carrier and fail the advance.
    pages = [
        {"title": "First", "fields": ["pick"], "kind": "input", "display": []},
        {"title": "Second", "fields": ["note"], "kind": "input", "display": []},
    ]
    await cache_endpoint_form(_TOKEN, _reacting_form(_CHIPS_SCHEMA, pages), datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "page_advanced", "tai42_page": "First", "pick": ["b"]},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    assert out["data"]["pick__val"] == ["b"]


async def test_reaction_values_set_a_single_pick_chips_init_as_a_one_element_list(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # A reaction that SETS a single-pick chips field emits its ``__init`` as a one-element list too,
    # so the ChipsSelector's array carrier is fed the shape it expects (not a scalar).
    await cache_endpoint_form(_TOKEN, _reacting_form(_CHIPS_SCHEMA, None), datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {"values": {"pick": "c"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "field_changed", "tai42_field": "note", "note": "hi"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["pick__init"] == ["c"]


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
    await cache_endpoint_form(_TOKEN, _reacting_form(schema, None), datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {"errors": {"qty": "must be at least 1"}}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {"tai42_event": "submitted", "qty": "0"},
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    # The recipient reads the human label the field renders, not the raw schema key.
    assert out["data"]["error_message"] == "Quantity: must be at least 1"


async def test_reaction_error_message_falls_back_to_the_key_without_a_title(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    schema = {
        "type": "object",
        "properties": {"qty": {"type": "integer"}},
        "required": ["qty"],
    }
    await cache_endpoint_form(_TOKEN, _reacting_form(schema, None), datetime.now(UTC) + timedelta(minutes=5))
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


async def test_page_advance_forwards_a_merged_range_val_carrier(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # The merged range control on the first screen returns one object value under the START's key; the
    # next-screen response forwards it as the START's ``__val`` carrier and emits no carrier for the
    # merged-away END.
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
            "note": {"type": "string"},
        },
    }
    pages = [
        {"title": "Dates", "fields": ["start", "end"], "kind": "input", "display": []},
        {"title": "Note", "fields": ["note"], "kind": "input", "display": []},
    ]
    await cache_endpoint_form(_TOKEN, _reacting_form(schema, pages), datetime.now(UTC) + timedelta(minutes=5))
    stub_app.interactions.react_result = {}
    payload = {
        "action": "data_exchange",
        "flow_token": _TOKEN,
        "screen": "SCREEN_A",
        "data": {
            "tai42_event": "page_advanced",
            "tai42_page": "Dates",
            "start": {"start-date": "2026-05-01", "end-date": "2026-05-04"},
        },
    }
    request, aes_key, iv = _request(payload)
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    assert out["data"]["start__val"] == {"start-date": "2026-05-01", "end-date": "2026-05-04"}
    assert "end__val" not in out["data"]


# --- the in-form shape check: a broken shape keeps the person on the screen ---------------

_SHAPE_TOKEN = "int-shape-1"
_SHAPE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "title": "Name"},
        "email": {"type": "string", "format": "email", "title": "Email address"},
        "qty": {"type": "integer", "minimum": 1, "title": "Quantity"},
        "note": {"type": "string", "pattern": "^[a-z]+$"},
    },
    "required": ["name", "email"],
}
_SHAPE_PAGES = [
    {"title": "Contact", "fields": ["name", "email"], "kind": "input", "display": []},
    {"title": "Amounts", "fields": ["qty", "note"], "kind": "input", "display": []},
]


def _shape_error(field: str, kind: FormShapeKind, message: str, **params: Any) -> FormShapeError:
    return FormShapeError(field=field, kind=kind, params=params, message=message)


async def _seed_shaped(*, reactions: dict[str, Any] | None = None, invalid: dict[str, str] | None = None) -> None:
    form = EndpointForm(
        schema=_SHAPE_SCHEMA,
        pages=_SHAPE_PAGES,
        reactions=reactions,
        option_fields=[],
        invalid=invalid or {},
        required={},
        lines={},
    )
    await cache_endpoint_form(_SHAPE_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))


def _shape_exchange(data: dict[str, Any], *, screen: str = "SCREEN_A", token: str = _SHAPE_TOKEN) -> dict[str, Any]:
    return {"action": "data_exchange", "flow_token": token, "screen": screen, "data": data}


# The entry screen's whole data, as its footer carries it to the endpoint.
_CONTACT_DATA = {
    "name__init": "",
    "name__err": "",
    "email__init": "",
    "email__err": "",
    "qty__init": "",
    "qty__err": "",
    "note__init": "",
    "note__err": "",
}
_SHAPE_ADVANCE = {
    "tai42_event": "page_advanced",
    "tai42_page": "Contact",
    "name": "alice",
    "email": "dhdhdh",
    **_CONTACT_DATA,
}
# The second screen's whole data, as its footer carries it to the endpoint.
_AMOUNTS_DATA = {
    "qty__init": "",
    "qty__err": "",
    "note__init": "",
    "note__err": "",
    "name__val": "alice",
    "email__val": "alice@example.org",
}


async def test_a_step_with_a_bad_email_stays_on_the_screen_with_the_field_sentence(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped()
    stub = stub_app.interactions
    stub.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Enter a valid email address", format="email")]
    request, aes_key, iv = _request(_shape_exchange(_SHAPE_ADVANCE))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    # The answer is the screen's whole data: what the footer carried, each control's value as the
    # person left it, and the refusal's sentences.
    assert out == {
        "screen": "SCREEN_A",
        "data": {
            **_CONTACT_DATA,
            "name__init": "alice",
            "email__init": "dhdhdh",
            "email__err": "Use name@example.com",
            "error_message": "Email address: Enter a valid email address",
        },
    }
    call = stub.shape_calls[-1]
    assert call["partial"] is True
    assert call["fields"] == ["name", "email"]
    assert call["values"] == {"name": "alice", "email": "dhdhdh"}
    assert call["question"].answer_format is AnswerFormat.FORM
    assert call["question"].format_payload["schema"] == _SHAPE_SCHEMA
    # No reaction is declared, so none runs.
    assert stub.react_calls == []


async def test_the_callers_sentence_wins_over_the_channel_word(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped(invalid={"email": "Use your work address"})
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Enter a valid email address")]
    request, aes_key, iv = _request(_shape_exchange(_SHAPE_ADVANCE))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["email__err"] == "Use your work address"
    assert out["data"]["error_message"] == "Email address: Use your work address"


async def test_a_clean_step_advances_to_a_next_screen_built_from_the_payload(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped()
    data = {**_SHAPE_ADVANCE, "email": "alice@example.org", "qty__init": "3", "email__err": "Use name@example.com"}
    request, aes_key, iv = _request(_shape_exchange(data))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out == {
        "screen": "SCREEN_B",
        "data": {
            "qty__init": "3",
            "qty__err": "",
            "note__init": "",
            "note__err": "",
            "name__val": "alice",
            "email__val": "alice@example.org",
        },
    }
    assert stub_app.interactions.react_calls == []


async def test_a_submit_with_a_bad_minimum_refuses_on_the_screen(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped()
    stub = stub_app.interactions
    stub.shape_errors = [_shape_error("qty", FormShapeKind.MINIMUM, "Enter a number of at least 1", minimum=1)]
    data = {
        "tai42_event": "submitted",
        "qty": "0",
        "note": "ok",
        "name": "alice",
        "email": "alice@example.org",
        **_AMOUNTS_DATA,
        "note__err": "Not in the expected form",
    }
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    # The earlier answers' carriers stay, each control keeps the person's value, and only the
    # failing field shows a sentence.
    assert out == {
        "screen": "SCREEN_B",
        "data": {
            **_AMOUNTS_DATA,
            "qty__init": "0",
            "note__init": "ok",
            "qty__err": channel_word(FormShapeKind.MINIMUM, {"minimum": 1}),
            "note__err": "",
            "error_message": "Quantity: Enter a number of at least 1",
        },
    }
    call = stub.shape_calls[-1]
    assert call["partial"] is False
    assert call["fields"] is None
    assert call["values"] == {"qty": 0, "note": "ok", "name": "alice", "email": "alice@example.org"}


async def test_a_clean_submit_completes_without_any_reaction(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped()
    data = {"tai42_event": "submitted", "qty": "2", "note": "", "name": "alice", "email": "alice@example.org"}
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"
    # An empty optional text field is unfilled: neither checked nor sent.
    assert out["data"]["extension_message_response"]["params"] == {
        "flow_token": _SHAPE_TOKEN,
        "Name": "alice",
        "Email address": "alice@example.org",
        "Quantity": 2,
    }
    assert stub_app.interactions.shape_calls[-1]["values"] == {"qty": 2, "name": "alice", "email": "alice@example.org"}
    assert stub_app.interactions.react_calls == []


async def test_a_clean_submit_a_reaction_refuses_clears_the_screens_field_sentences(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped(reactions={"submitted": True})
    stub_app.interactions.react_result = {"errors": {"qty": "sold out"}}
    data = {
        "tai42_event": "submitted",
        "qty": "2",
        "note": "ok",
        "name": "alice",
        "email": "alice@example.org",
        **_AMOUNTS_DATA,
        "qty__err": "Enter a number",
    }
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out == {
        "screen": "SCREEN_B",
        "data": {**_AMOUNTS_DATA, "qty__init": "2", "note__init": "ok", "error_message": "Quantity: sold out"},
    }


async def test_a_declared_reaction_runs_only_after_a_clean_shape_check(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped(reactions={"page_advanced": ["Contact"], "submitted": True})
    stub = stub_app.interactions
    stub.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Enter a valid email address")]
    request, aes_key, iv = _request(_shape_exchange(_SHAPE_ADVANCE))
    _decrypt_response(await flow_handler(request), aes_key, iv)
    assert stub.react_calls == []
    stub.shape_errors = []
    request, aes_key, iv = _request(_shape_exchange({**_SHAPE_ADVANCE, "email": "alice@example.org"}))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    assert stub.react_calls[-1]["event"] == PageAdvancedEvent(page="Contact")
    data = {"tai42_event": "submitted", "qty": "2", "name": "alice", "email": "alice@example.org"}
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"
    assert stub.react_calls[-1]["event"] == SubmittedEvent()


async def test_an_undeclared_page_advance_is_a_shape_check_only_advance(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_shaped(reactions={"page_advanced": ["Amounts"]})
    request, aes_key, iv = _request(_shape_exchange({**_SHAPE_ADVANCE, "email": "alice@example.org"}))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    assert stub_app.interactions.react_calls == []


async def test_a_picker_fed_per_send_is_never_refused_for_membership(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    schema = {"type": "object", "properties": {"slot": {"type": "string", "enum": ["9am"]}, "on": {"type": "string"}}}
    form = EndpointForm(
        schema=schema, pages=None, reactions=None, option_fields=["slot"], invalid={}, required={}, lines={}
    )
    await cache_endpoint_form(_SHAPE_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))
    # The per-send list the picker drew replaced the schema enum; the door judges membership.
    stub_app.interactions.shape_errors = [_shape_error("slot", FormShapeKind.ENUM, "Choose one of the offered options")]
    request, aes_key, iv = _request(_shape_exchange({"tai42_event": "submitted", "slot": "10am"}))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"


async def test_a_notify_form_resolves_its_sidecar_and_reads_its_sentences_from_the_payload(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, monkeypatch
):
    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", "waba-1")
    reset_all_settings()
    names = {"Name": "name", "Email address": "email", "Quantity": "qty", "note": "note"}
    await cache_flow_form("waba-1", "hash1", _SHAPE_SCHEMA, names, _SHAPE_PAGES, [])
    token = "tai42-nf:hash1:nonce1"
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Enter a valid email address")]
    data = {**_SHAPE_ADVANCE, "email__invalid": "Use your work address", "qty__invalid": ""}
    request, aes_key, iv = _request(_shape_exchange(data, token=token))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["email__err"] == "Use your work address"
    stub_app.interactions.shape_errors = []
    data = {"tai42_event": "submitted", "qty": "2", "name": "alice", "email": "a@b.co", "qty__invalid": ""}
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B", token=token))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"
    assert out["data"]["extension_message_response"]["params"]["flow_token"] == token
    assert stub_app.interactions.react_calls == []


async def test_a_notify_token_with_no_sidecar_is_427(flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis):
    request, _, _ = _request(_shape_exchange(_SHAPE_ADVANCE, token="tai42-nf:missing:nonce"))
    response = await flow_handler(request)
    assert response.status_code == 427


# --- a merged range pair: one error object carries each end's sentence -------------------

_RANGE_TOKEN = "int-range-1"
_RANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "title": "Name"},
        "start": {"type": "string", "format": "date", "title": "From"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 2, "title": "To"},
        "note": {"type": "string", "pattern": "^[a-z]+$"},
    },
}
_RANGE_FIRST = [
    {"title": "Dates", "fields": ["name", "start", "end"], "kind": "input", "display": []},
    {"title": "Notes", "fields": ["note"], "kind": "input", "display": []},
]
_RANGE_LATER = [
    {"title": "Contact", "fields": ["name"], "kind": "input", "display": []},
    {"title": "Dates", "fields": ["start", "end", "note"], "kind": "input", "display": []},
]
_NO_RANGE_ERROR = {"start-date": "", "end-date": ""}
_PICKED = {"start-date": "2026-05-04", "end-date": "2026-05-01"}
# The entry screen's whole data with the pair on it, as its footer carries it to the endpoint.
_DATES_DATA = {
    "name__init": "",
    "name__err": "",
    "start__init": _NO_RANGE_ERROR,
    "start__err": _NO_RANGE_ERROR,
    "note__init": "",
    "note__err": "",
}
_DATES_ADVANCE = {
    "tai42_event": "page_advanced",
    "tai42_page": "Dates",
    "name": "alice",
    "start": _PICKED,
    **_DATES_DATA,
}


async def _seed_range(
    pages: list[dict[str, Any]], *, reactions: dict[str, Any] | None = None, invalid: dict[str, str] | None = None
) -> None:
    form = EndpointForm(
        schema=_RANGE_SCHEMA,
        pages=pages,
        reactions=reactions,
        option_fields=[],
        invalid=invalid or {},
        required={},
        lines={},
    )
    await cache_endpoint_form(_RANGE_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))


def _errors_of(out: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in out["data"].items() if key.endswith("__err")}


async def test_a_range_start_error_shows_on_the_start_end_of_the_one_error_object(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_range(_RANGE_FIRST, invalid={"start": "Pick a later first day"})
    stub_app.interactions.shape_errors = [_shape_error("start", FormShapeKind.MIN_DATE, "Too early")]
    request, aes_key, iv = _request(_shape_exchange(_DATES_ADVANCE, token=_RANGE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert _errors_of(out) == {
        "name__err": "",
        "start__err": {"start-date": "Pick a later first day", "end-date": ""},
        "note__err": "",
    }
    assert out["data"]["start__init"] == _PICKED
    assert out["data"]["error_message"] == "From: Pick a later first day"


async def test_a_range_end_error_shows_on_the_end_end_of_the_one_error_object(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_range(_RANGE_FIRST)
    stub_app.interactions.shape_errors = [_shape_error("end", FormShapeKind.RANGE_ORDER, "The end is before the start")]
    request, aes_key, iv = _request(_shape_exchange(_DATES_ADVANCE, token=_RANGE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert _errors_of(out) == {
        "name__err": "",
        "start__err": {"start-date": "", "end-date": channel_word(FormShapeKind.RANGE_ORDER, {})},
        "note__err": "",
    }
    assert out["data"]["error_message"] == "To: The end is before the start"


async def test_errors_on_both_range_ends_show_each_on_its_own_end(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_range(_RANGE_FIRST, invalid={"end": "Pick a later last day"})
    stub_app.interactions.shape_errors = [
        _shape_error("start", FormShapeKind.REQUIRED, "Required"),
        _shape_error("end", FormShapeKind.MIN_DATE, "Too early"),
        _shape_error("name", FormShapeKind.REQUIRED, "Required"),
    ]
    request, aes_key, iv = _request(_shape_exchange(_DATES_ADVANCE, token=_RANGE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert _errors_of(out) == {
        "name__err": channel_word(FormShapeKind.REQUIRED, {}),
        "start__err": {"start-date": channel_word(FormShapeKind.REQUIRED, {}), "end-date": "Pick a later last day"},
        "note__err": "",
    }


async def test_a_clean_range_step_a_reaction_refuses_clears_the_range_error_object(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_range(_RANGE_FIRST, reactions={"page_advanced": ["Dates"]})
    stub_app.interactions.react_result = {"errors": {"start": "not available"}}
    data = {**_DATES_ADVANCE, "start__err": {"start-date": "Too early", "end-date": "Too early"}, "name__err": "Bad"}
    request, aes_key, iv = _request(_shape_exchange(data, token=_RANGE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert _errors_of(out) == {"name__err": "", "start__err": _NO_RANGE_ERROR, "note__err": ""}
    assert out["data"]["error_message"] == "From: not available"


async def test_a_range_on_a_later_screen_receives_its_error_object_forwarded(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_range(_RANGE_LATER)
    data = {"tai42_event": "page_advanced", "tai42_page": "Contact", "name": "alice", **_DATES_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token=_RANGE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out == {
        "screen": "SCREEN_B",
        "data": {
            "start__init": _NO_RANGE_ERROR,
            "start__err": _NO_RANGE_ERROR,
            "note__init": "",
            "note__err": "",
            "name__val": "alice",
        },
    }


async def test_a_range_error_on_a_later_screen_fills_its_one_error_object(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_range(_RANGE_LATER)
    stub_app.interactions.shape_errors = [_shape_error("end", FormShapeKind.RANGE_SPAN, "Too short a span")]
    data = {
        "tai42_event": "submitted",
        "name": "alice",
        "start": _PICKED,
        "start__init": _NO_RANGE_ERROR,
        "start__err": _NO_RANGE_ERROR,
        "note__init": "",
        "note__err": "",
        "name__val": "alice",
    }
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B", token=_RANGE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    assert _errors_of(out) == {
        "start__err": {"start-date": "", "end-date": channel_word(FormShapeKind.RANGE_SPAN, {})},
        "note__err": "",
    }


# A form whose required text field and required date are each shown for several values of one
# controller, and a note shown on one value of the split text field (a chained split). Each split
# field is drawn as one control per case (``target__a``/``target__b``, ``day__a``/``day__c``,
# ``note__a``/``note__b``), which alone makes the form endpoint-driven; the second screen asks an email.
_MULTI_TOKEN = "int-multi-1"
_MULTI_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "title": "Kind", "enum": ["a", "b", "c"]},
        "target": {"type": "string", "title": "Target", "visibleWhen": {"field": "kind", "in": ["a", "b"]}},
        "day": {"type": "string", "title": "Day", "format": "date", "visibleWhen": {"field": "kind", "in": ["a", "c"]}},
        "note": {"type": "string", "title": "Note", "visibleWhen": {"field": "target", "equals": "x"}},
        "email": {"type": "string", "format": "email", "title": "Email"},
    },
    "required": ["kind", "target", "day"],
}
_MULTI_PAGES = [
    {"title": "First", "fields": ["kind", "target", "day", "note"], "kind": "input", "display": []},
    {"title": "Second", "fields": ["email"], "kind": "input", "display": []},
]


async def _seed_multi(
    pages: list[dict[str, Any]] | None, schema: dict[str, Any] = _MULTI_SCHEMA
) -> list[dict[str, Any]]:
    """Cache the multi-value form and return its rendered screens."""
    form = EndpointForm(schema=schema, pages=pages, reactions=None, option_fields=[], invalid={}, required={}, lines={})
    await cache_endpoint_form(_MULTI_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))
    return build_form_flow(schema, pages)[0]["screens"]


@pytest.mark.parametrize("undrawn", ["", "stale"])
async def test_a_step_hands_the_shown_multi_value_fields_answers_to_the_check(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, undrawn: str
):
    # Whatever an undrawn case carries, the shown case's value is the field's answer.
    screens = await _seed_multi(_MULTI_PAGES)
    entered = {"kind": "a", "target__a": "x", "day__a": "2026-01-05", "note__a": "n"}
    data = phone_footer_payload(screens[0], entered, undrawn=undrawn)
    request, aes_key, iv = _request(_shape_exchange(data, token=_MULTI_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert stub_app.interactions.shape_calls[-1]["values"] == {
        "kind": "a",
        "target": "x",
        "day": "2026-01-05",
        "note": "n",
    }
    assert out["screen"] == "SCREEN_B"
    assert {key: out["data"][key] for key in ("kind__val", "target__val", "day__val", "note__val")} == {
        "kind__val": "a",
        "target__val": "x",
        "day__val": "2026-01-05",
        "note__val": "n",
    }
    # No per-case entry rides on into the next screen's data.
    assert not [key for key in out["data"] if key.startswith(("target__a", "target__b", "day__a", "day__c"))]


async def test_a_step_reads_a_chained_split_fields_answer_from_its_controllers_shown_case(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    screens = await _seed_multi(_MULTI_PAGES)
    entered = {"kind": "b", "target__b": "x", "note__b": "m", "target__a": "y", "note__a": "old"}
    request, _, _ = _request(
        _shape_exchange(phone_footer_payload(screens[0], entered, undrawn="stale"), token=_MULTI_TOKEN)
    )
    await flow_handler(request)
    assert stub_app.interactions.shape_calls[-1]["values"] == {"kind": "b", "target": "x", "note": "m"}


async def test_a_step_leaves_a_hidden_multi_value_field_unfilled(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    screens = await _seed_multi(_MULTI_PAGES)
    entered = {"kind": "c", "target__a": "stale", "day__c": "2026-01-05"}
    data = phone_footer_payload(screens[0], entered, undrawn="stale")
    request, aes_key, iv = _request(_shape_exchange(data, token=_MULTI_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert stub_app.interactions.shape_calls[-1]["values"] == {"kind": "c", "day": "2026-01-05"}
    assert out["screen"] == "SCREEN_B"
    assert out["data"]["target__val"] == ""


async def test_a_step_with_a_shown_multi_value_field_left_empty_hands_no_value_to_the_check(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # An empty shown field is unfilled: the check receives no value for it (a step's check is partial,
    # so the platform does not demand it until the submit).
    screens = await _seed_multi(_MULTI_PAGES)
    data = phone_footer_payload(screens[0], {"kind": "b", "target__b": ""}, undrawn="stale")
    request, _, _ = _request(_shape_exchange(data, token=_MULTI_TOKEN))
    await flow_handler(request)
    call = stub_app.interactions.shape_calls[-1]
    assert (call["partial"], call["values"]) == (True, {"kind": "b"})


async def test_a_request_from_a_screen_outside_the_form_reads_no_per_case_answer(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # No screen of the form names the cases the request could carry, so none is read as an answer.
    screens = await _seed_multi(_MULTI_PAGES)
    data = phone_footer_payload(screens[0], {"kind": "a", "target__a": "x"}, undrawn="stale")
    request, _, _ = _request(_shape_exchange(data, screen="ELSEWHERE", token=_MULTI_TOKEN))
    await flow_handler(request)
    assert stub_app.interactions.shape_calls[-1]["values"] == {"kind": "a"}


async def test_a_refused_step_puts_the_shown_cases_value_back_into_the_shared_init_carrier(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # Every case of a split field reads the one ``<field>__init`` carrier, so a refusal that keeps the
    # person on the screen hands it the shown case's value (never an empty one).
    screens = await _seed_multi(None)
    stub = stub_app.interactions
    stub.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Not an email")]
    entered = {"kind": "a", "target__a": "x", "day__a": "2026-01-05", "email": "nope"}
    request, aes_key, iv = _request(_shape_exchange(phone_footer_payload(screens[0], entered), token=_MULTI_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert (out["data"]["target__init"], out["data"]["day__init"]) == ("x", "2026-01-05")
    assert not [key for key in out["data"] if key in ("target__a", "target__b", "day__a", "day__c")]


async def test_a_submit_hands_the_shown_multi_value_fields_answers_to_the_check_and_completes(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    screens = await _seed_multi(None)
    entered = {"kind": "a", "target__a": "x", "day__a": "2026-01-05", "email": "alice@example.org"}
    request, aes_key, iv = _request(
        _shape_exchange(phone_footer_payload(screens[0], entered, undrawn="stale"), token=_MULTI_TOKEN)
    )
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert stub_app.interactions.shape_calls[-1]["values"] == {
        "kind": "a",
        "target": "x",
        "day": "2026-01-05",
        "email": "alice@example.org",
    }
    assert out["screen"] == "SUCCESS"
    assert out["data"]["extension_message_response"]["params"] == {
        "flow_token": _MULTI_TOKEN,
        "Kind": "a",
        "Target": "x",
        "Day": "2026-01-05",
        "Email": "alice@example.org",
    }


async def test_a_submit_with_a_shown_multi_value_date_left_empty_is_refused_as_required(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    screens = await _seed_multi(None)
    stub = stub_app.interactions
    stub.shape_errors = [_shape_error("day", FormShapeKind.REQUIRED, "This field is required")]
    entered = {"kind": "c", "day__c": "", "email": "alice@example.org"}
    request, aes_key, iv = _request(
        _shape_exchange(phone_footer_payload(screens[0], entered, undrawn="stale"), token=_MULTI_TOKEN)
    )
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert stub.shape_calls[-1]["values"] == {"kind": "c", "email": "alice@example.org"}
    assert out["screen"] == "SCREEN_A"
    assert out["data"]["error_message"] == "Day: This field is required"


# A split field on a later screen, gated on a controller collected on the first screen.
_LATER_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "title": "Kind", "enum": ["a", "b", "c"]},
        "later": {"type": "string", "title": "Later", "visibleWhen": {"field": "kind", "in": ["a", "b"]}},
    },
    "required": ["kind", "later"],
}
_LATER_PAGES = [
    {"title": "First", "fields": ["kind"], "kind": "input", "display": []},
    {"title": "Second", "fields": ["later"], "kind": "input", "display": []},
]


async def test_a_submit_reads_a_later_screens_split_field_from_the_earlier_controllers_value(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    screens = await _seed_multi(_LATER_PAGES, _LATER_SCHEMA)
    carried = {"kind__val": "b", "later__init": "", "later__err": ""}
    data = phone_footer_payload(screens[1], {"later__b": "y"}, carried, undrawn="stale")
    request, aes_key, iv = _request(_shape_exchange(data, screen="SCREEN_B", token=_MULTI_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert stub_app.interactions.shape_calls[-1]["values"] == {"kind": "b", "later": "y"}
    assert out["data"]["extension_message_response"]["params"] == {
        "flow_token": _MULTI_TOKEN,
        "Kind": "b",
        "Later": "y",
    }


async def test_a_field_change_on_a_split_field_hands_the_shown_cases_value_to_the_reaction(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "title": "Kind", "enum": ["a", "b", "c"]},
            "size": {
                "type": "string",
                "title": "Size",
                "enum": ["s", "m"],
                "visibleWhen": {"field": "kind", "in": ["a", "b"]},
            },
        },
    }
    reactions = {"field_changed": ["size"], "page_advanced": [], "submitted": False}
    form = EndpointForm(
        schema=schema, pages=None, reactions=reactions, option_fields=[], invalid={}, required={}, lines={}
    )
    await cache_endpoint_form(_MULTI_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))
    screen = build_form_flow(schema, None, reactions=reactions)[0]["screens"][0]
    case_b = next(
        node["then"][0]
        for node in screen["layout"]["children"]
        if node.get("type") == "If" and node["then"][0]["name"] == "size__b"
    )
    payload = case_b["on-select-action"]["payload"]
    data = phone_payload(screen, payload, {"kind": "b", "size__b": "m"}, undrawn="stale")
    request, _, _ = _request(_shape_exchange(data, token=_MULTI_TOKEN))
    await flow_handler(request)
    assert stub_app.interactions.react_calls[-1]["partial_values"] == {"kind": "b", "size": "m"}


# --- a range with a time on each end: drawn, refused and summarised in drawing order -----------

_TIMED_TOKEN = "int-timed-1"
# The times are declared LAST in the schema; each draws right after its date.
_TIMED_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date", "title": "From"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "To"},
        "note": {"type": "string", "title": "Note"},
        "start_time": {"type": "string", "format": "time", "title": "From time", "timeOf": "start"},
        "end_time": {"type": "string", "format": "time", "title": "To time", "timeOf": "end"},
    },
}
_TIMED_PAGES = [
    {"title": "Dates", "fields": ["start", "end", "start_time", "end_time"], "kind": "input", "display": []},
    {"title": "Notes", "fields": ["note"], "kind": "input", "display": []},
]


def _timed_form(pages: list[dict[str, Any]] | None) -> EndpointForm:
    return EndpointForm(
        schema=_TIMED_SCHEMA, pages=pages, reactions=None, option_fields=[], invalid={}, required={}, lines={}
    )


async def _seed_timed(pages: list[dict[str, Any]] | None) -> None:
    await cache_endpoint_form(_TIMED_TOKEN, _timed_form(pages), datetime.now(UTC) + timedelta(minutes=5))


def test_screen_fields_lists_each_time_right_after_its_date():
    fields_by_screen, current = _screen_fields(_timed_form(_TIMED_PAGES), "SCREEN_A")
    assert current == 0
    assert fields_by_screen == [["start", "start_time", "end", "end_time"], ["note"]]
    page_less, _ = _screen_fields(_timed_form(None), "SCREEN_A")
    assert page_less == [["start", "start_time", "end", "end_time", "note"]]


# A field declared BETWEEN the two range ends: the merged END and its time still follow the START's time.
_SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date", "title": "From"},
        "note": {"type": "string", "title": "Note"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "To"},
        "start_time": {"type": "string", "format": "time", "title": "From time", "timeOf": "start"},
        "end_time": {"type": "string", "format": "time", "title": "To time", "timeOf": "end"},
    },
}


def test_screen_fields_put_a_merged_end_and_its_time_after_the_start_time():
    form = EndpointForm(
        schema=_SPLIT_SCHEMA, pages=None, reactions=None, option_fields=[], invalid={}, required={}, lines={}
    )
    fields_by_screen, _ = _screen_fields(form, "SCREEN_A")
    assert fields_by_screen == [["start", "start_time", "end", "end_time", "note"]]


async def test_a_clean_submit_lists_a_merged_end_time_label_after_the_start_time_label(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    form = EndpointForm(
        schema=_SPLIT_SCHEMA, pages=None, reactions=None, option_fields=[], invalid={}, required={}, lines={}
    )
    await cache_endpoint_form(_TIMED_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))
    data = {
        "tai42_event": "submitted",
        "start": {"start-date": "2026-05-01", "end-date": "2026-05-02"},
        "note": "hello",
        "start_time": "09:00",
        "end_time": "10:00",
    }
    request, aes_key, iv = _request(_shape_exchange(data, token=_TIMED_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"
    params = out["data"]["extension_message_response"]["params"]
    assert list(params) == ["flow_token", "From", "From time", "To", "To time", "Note"]


async def test_a_same_day_earlier_end_time_at_a_step_is_refused_on_the_end_time_control(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_timed(_TIMED_PAGES)
    stub_app.interactions.shape_errors = [
        _shape_error(
            "end_time",
            FormShapeKind.RANGE_ORDER,
            "The end time must be on or after the start time",
            rangeStart="start",
            by="time",
        )
    ]
    data = {
        "tai42_event": "page_advanced",
        "tai42_page": "Dates",
        "start": {"start-date": "2026-05-01", "end-date": "2026-05-01"},
        "start_time": "10:00",
        "end_time": "09:00",
        "start__init": _NO_RANGE_ERROR,
        "start__err": _NO_RANGE_ERROR,
        "start_time__init": "",
        "start_time__err": "",
        "end_time__init": "",
        "end_time__err": "",
        "note__init": "",
        "note__err": "",
    }
    request, aes_key, iv = _request(_shape_exchange(data, token=_TIMED_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert stub_app.interactions.shape_calls[0]["fields"] == ["start", "start_time", "end", "end_time"]
    assert out["data"]["end_time__err"] == "End time is before start"
    assert out["data"]["start_time__err"] == ""
    assert out["data"]["start__err"] == _NO_RANGE_ERROR
    assert out["data"]["error_message"] == "To time: The end time must be on or after the start time"


async def test_a_clean_submit_of_a_timed_range_completes_with_the_labels_in_drawing_order(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_timed(None)
    data = {
        "tai42_event": "submitted",
        "start": {"start-date": "2026-05-01", "end-date": "2026-05-02"},
        "start_time": "09:00",
        "end_time": "10:00",
        "note": "hello",
    }
    request, aes_key, iv = _request(_shape_exchange(data, token=_TIMED_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SUCCESS"
    params = out["data"]["extension_message_response"]["params"]
    assert list(params) == ["flow_token", "From", "From time", "To", "To time", "Note"]
    assert params["From"] == "2026-05-01"
    assert params["To"] == "2026-05-02"


# --- the caller's form lines: the pop-up summary, the could-not-update line, the required sentence ---

_LINE_TOKEN = "int-lines-1"
_LINE_DEFAULT_NOTICE = "Sorry, this form could not be updated right now. Please try again."


async def _seed_lines(
    *,
    lines: dict[str, str] | None = None,
    required: dict[str, str] | None = None,
    reactions: dict[str, Any] | None = None,
) -> None:
    form = EndpointForm(
        schema=_SHAPE_SCHEMA,
        pages=_SHAPE_PAGES,
        reactions=reactions,
        option_fields=[],
        invalid={},
        required=required or {},
        lines=lines or {},
    )
    await cache_endpoint_form(_LINE_TOKEN, form, datetime.now(UTC) + timedelta(minutes=5))


def _all_triggers() -> dict[str, Any]:
    return {
        "field_changed": list(_SHAPE_SCHEMA["properties"]),
        "page_advanced": [page["title"] for page in _SHAPE_PAGES],
        "submitted": True,
        "choices": [],
    }


async def test_an_ask_shape_refusal_shows_the_callers_error_line(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(lines={"error_line": "Please check the form"})
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Enter a valid email address")]
    request, aes_key, iv = _request(_shape_exchange(_SHAPE_ADVANCE, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "Please check the form"
    # The field's own sentence does not change: only the pop-up summary takes the caller's line.
    assert out["data"]["email__err"] == channel_word(FormShapeKind.FORMAT, {})


async def test_an_ask_less_shape_refusal_shows_the_carried_error_line(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, monkeypatch
):
    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", "waba-1")
    reset_all_settings()
    names = {"Name": "name", "Email address": "email", "Quantity": "qty", "note": "note"}
    await cache_flow_form("waba-1", "hash1", _SHAPE_SCHEMA, names, _SHAPE_PAGES, [])
    token = "tai42-nf:hash1:nonce1"
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.FORMAT, "Enter a valid email address")]
    data = {**_SHAPE_ADVANCE, "tai42_error_line": "Please check the form"}
    request, aes_key, iv = _request(_shape_exchange(data, token=token))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "Please check the form"
    # The carrier survives the echo, so the screen keeps the line for the next exchange.
    assert out["data"]["tai42_error_line"] == "Please check the form"
    data = {**_SHAPE_ADVANCE, "tai42_error_line": ""}
    request, aes_key, iv = _request(_shape_exchange(data, token=token))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "Email address: Enter a valid email address"


async def test_a_field_changed_reaction_error_shows_the_callers_error_line(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(lines={"error_line": "Please check the form"}, reactions=_all_triggers())
    stub_app.interactions.react_result = {"errors": {"email": "already taken"}}
    data = {"tai42_event": "field_changed", "tai42_field": "email", "email": "a@b.co", **_CONTACT_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "Please check the form"


async def test_a_page_advanced_reaction_error_shows_the_callers_error_line(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(lines={"error_line": "Please check the form"}, reactions=_all_triggers())
    stub_app.interactions.react_result = {"errors": {"email": "already taken"}}
    data = {**_SHAPE_ADVANCE, "email": "alice@example.org"}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_A"
    assert out["data"]["error_message"] == "Please check the form"


async def test_a_reaction_error_without_a_line_keeps_the_label_list(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(reactions=_all_triggers())
    stub_app.interactions.react_result = {"errors": {"email": "already taken", "name": "too short"}}
    data = {**_SHAPE_ADVANCE, "email": "alice@example.org"}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "Email address: already taken; Name: too short"


_FAILED_TEMPLATE = "We could not refresh the form ({reason})."


async def test_a_reaction_door_error_fills_the_callers_update_failed_with_its_reason(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(lines={"update_failed": _FAILED_TEMPLATE}, reactions=_all_triggers())
    stub_app.interactions.react_error = FormReactionHandlerError("the lookup timed out")
    data = {"tai42_event": "field_changed", "tai42_field": "email", "email": "a@b.co", **_CONTACT_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "We could not refresh the form (the lookup timed out)."


async def test_a_reaction_door_error_without_a_line_keeps_the_default_notice(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(reactions=_all_triggers())
    stub_app.interactions.react_error = FormReactionHandlerError("the lookup timed out")
    data = {"tai42_event": "field_changed", "tai42_field": "email", "email": "a@b.co", **_CONTACT_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == _LINE_DEFAULT_NOTICE


@pytest.mark.parametrize(
    ("case", "react_error"),
    [
        ("undeclared trigger", None),
        ("no event marker", None),
        ("unrenderable update", None),
        ("any other failure", RuntimeError("handler blew up")),
    ],
)
async def test_every_other_failure_door_draws_the_callers_update_failed_with_no_reason(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, case: str, react_error: BaseException | None
):
    reactions = _all_triggers()
    if case == "undeclared trigger":
        reactions["field_changed"] = ["name"]
    await _seed_lines(lines={"update_failed": _FAILED_TEMPLATE}, reactions=reactions)
    stub_app.interactions.react_error = react_error
    if case == "unrenderable update":
        # A replaced option whose second line is past Meta's 300-character cap cannot be rendered.
        stub_app.interactions.react_result = {
            "options": {"name": [{"value": "a", "label": "A", "description": "d" * 301}]}
        }
    data: dict[str, Any] = {"tai42_event": "field_changed", "tai42_field": "email", "email": "a@b.co", **_CONTACT_DATA}
    if case == "no event marker":
        data = {"email": "a@b.co", **_CONTACT_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "We could not refresh the form ()."


async def test_an_ask_less_failure_draws_the_carried_update_failed(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, monkeypatch
):
    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", "waba-1")
    reset_all_settings()
    names = {"Name": "name", "Email address": "email", "Quantity": "qty", "note": "note"}
    await cache_flow_form("waba-1", "hash1", _SHAPE_SCHEMA, names, _SHAPE_PAGES, [])
    data = {"email": "a@b.co", "tai42_update_failed": "Not refreshed: {reason}", **_CONTACT_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token="tai42-nf:hash1:nonce1"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == "Not refreshed: "
    data = {"email": "a@b.co", "tai42_update_failed": "", **_CONTACT_DATA}
    request, aes_key, iv = _request(_shape_exchange(data, token="tai42-nf:hash1:nonce1"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["error_message"] == _LINE_DEFAULT_NOTICE


async def test_a_missing_value_shows_the_callers_required_sentence(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines(required={"email": "We need your email"})
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.REQUIRED, "This field is required")]
    request, aes_key, iv = _request(_shape_exchange(_SHAPE_ADVANCE, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["email__err"] == "We need your email"
    assert out["data"]["error_message"] == "Email address: We need your email"


async def test_a_missing_value_without_a_required_sentence_keeps_the_channel_word(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    # A caller ``invalid`` sentence words a broken shape, never a missing value.
    await _seed_lines()
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.REQUIRED, "This field is required")]
    request, aes_key, iv = _request(_shape_exchange(_SHAPE_ADVANCE, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["email__err"] == channel_word(FormShapeKind.REQUIRED, {})
    assert out["data"]["error_message"] == "Email address: This field is required"


async def test_an_ask_less_missing_value_shows_the_carried_required_sentence(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis, monkeypatch
):
    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", "waba-1")
    reset_all_settings()
    names = {"Name": "name", "Email address": "email", "Quantity": "qty", "note": "note"}
    await cache_flow_form("waba-1", "hash1", _SHAPE_SCHEMA, names, _SHAPE_PAGES, [])
    stub_app.interactions.shape_errors = [_shape_error("email", FormShapeKind.REQUIRED, "This field is required")]
    data = {**_SHAPE_ADVANCE, "email__required": "We need your email", "email__invalid": "Not this one"}
    request, aes_key, iv = _request(_shape_exchange(data, token="tai42-nf:hash1:nonce1"))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["data"]["email__err"] == "We need your email"


async def test_a_clean_advance_forwards_the_form_lines_and_required_carriers(
    flow_env, flow_handler, stub_app: _StubApp, fake_redis: FakeRedis
):
    await _seed_lines()
    carried = {
        "tai42_error_line": "Please check the form",
        "tai42_update_failed": "Not refreshed: {reason}",
        "name__required": "We need your name",
        "email__required": "",
    }
    data = {**_SHAPE_ADVANCE, "email": "alice@example.org", **carried}
    request, aes_key, iv = _request(_shape_exchange(data, token=_LINE_TOKEN))
    out = _decrypt_response(await flow_handler(request), aes_key, iv)
    assert out["screen"] == "SCREEN_B"
    for key, value in carried.items():
        assert out["data"][key] == value
