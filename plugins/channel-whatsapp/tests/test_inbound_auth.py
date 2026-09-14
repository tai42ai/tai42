"""The inbound webhook door authentication — GET verification handshake and the
fail-closed POST signature check."""

from __future__ import annotations

import json

import pytest

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    FakeHttpx,
    FakeRedis,
    build_request,
    compute_signature,
    message_payload,
    signed_request,
    verify_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


# --- GET verification handshake -----------------------------------------------


async def test_verify_valid_token_echoes_challenge(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(challenge="12345"))

    assert result.status_code == 200
    assert result.body == b"12345"


async def test_verify_bad_token_is_403(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(token="wrong-token", challenge="12345"))

    assert result.status_code == 403


async def test_verify_non_ascii_token_is_403_not_500(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A non-ASCII hub.verify_token must map to a 403 mismatch, never a
    # compare_digest TypeError surfacing as a 500; the challenge is not echoed.
    query = b"hub.mode=subscribe&hub.verify_token=\xff&hub.challenge=12345"
    request = build_request(method="GET", headers={"host": "public.example"}, query=query)

    result = await handler(request)

    assert result.status_code == 403
    assert result.body != b"12345"


async def test_verify_wrong_mode_is_403(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(mode="unsubscribe"))

    assert result.status_code == 403


async def test_verify_missing_challenge_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(challenge=None))

    assert result.status_code == 400


async def test_verify_unset_token_is_500(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_VERIFY_TOKEN")
    reset_all_settings()

    result = await handler(verify_request(challenge="12345"))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}


# --- POST signature (fail-closed) ---------------------------------------------


async def _assert_rejected(handler, request, status: int = 401):
    result = await handler(request)
    assert result.status_code == status
    return result


async def test_missing_signature_header_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _assert_rejected(handler, signed_request(message_payload(), omit_signature=True))
    assert stub_app.conversations.accept_calls == []


async def test_wrong_secret_signature_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _assert_rejected(handler, signed_request(message_payload(), secret="not-the-secret"))
    assert stub_app.conversations.accept_calls == []


async def test_malformed_signature_header_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A header not in sha256=<hex> form is rejected before any HMAC compare.
    await _assert_rejected(handler, signed_request(message_payload(), signature="deadbeef"))
    assert stub_app.conversations.accept_calls == []


async def test_non_hex_signature_rejected_401_not_500(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A sha256=<non-hex/non-ASCII> header must decode-fail to a 401, never raise a
    # compare_digest TypeError that surfaces as a 500.
    await _assert_rejected(handler, signed_request(message_payload(), signature="sha256=\xff"))
    await _assert_rejected(handler, signed_request(message_payload(), signature="sha256=zz"))
    assert stub_app.conversations.accept_calls == []


async def test_tampered_body_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Sign one body, deliver another: the signature is over the RAW bytes.
    signature = compute_signature(json.dumps(message_payload(text="yes")).encode("utf-8"))
    request = signed_request(message_payload(text="no way"), signature=signature)
    await _assert_rejected(handler, request)
    assert stub_app.conversations.accept_calls == []


async def test_unset_app_secret_is_500(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_APP_SECRET")
    reset_all_settings()

    # Operator misconfiguration is a logged, constant 500 — never a 401 that reads
    # like an ordinary bad signature, never any processing.
    result = await handler(signed_request(message_payload()))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}
    assert stub_app.conversations.accept_calls == []


async def test_oversized_body_413_before_any_signature_work(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # No signature header at all, yet the response is 413 (not 401): the bounded
    # read runs BEFORE any HMAC work.
    request = build_request(
        method="POST",
        chunks=[b"x" * (512 * 1024), b"y" * (512 * 1024), b"z"],
        headers={"host": "public.example", "content-type": "application/json"},
    )
    await _assert_rejected(handler, request, status=413)


async def test_invalid_json_after_valid_signature_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    body = b"{not json"
    request = build_request(
        method="POST",
        body=body,
        headers={
            "host": "public.example",
            "content-type": "application/json",
            "x-hub-signature-256": compute_signature(body),
        },
    )
    result = await handler(request)
    assert result.status_code == 400
