"""Inbound entry-params vocabulary (bridge-path only).

Every param below rides ONLY on the conversation-bridge path (a fresh turn via
``conversations.accept``); the correlated-answer path forwards ``{"answer": …}`` to
the callback door — a seam that carries no params — so a tap/button that ANSWERS a
pending question surfaces none.
"""

from __future__ import annotations

import pytest
from tai42_contract.channels import InboundAnswerOutcome

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    _SEEN_KEY,
    _WAMID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _params_envelope,
    _seed_pending_select,
    interactive_payload,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


async def test_button_reply_tap_bridges_reply_id_in_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A button_reply tap with NO pending question bridges the tap's
    # title AND carries the tapped wire id under params.reply_id; without it the
    # bridge accept would pass no params, leaving params None.
    result = await handler(signed_request(interactive_payload(reply_id="int-9:2", title="Talk to sales")))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Talk to sales"
    assert call["params"] == {"reply_id": "int-9:2"}
    assert _SEEN_KEY in fake_redis.store


async def test_list_reply_tap_bridges_reply_id_and_description(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A list pick with a description carries both reply_id and reply_description.
    result = await handler(
        signed_request(
            interactive_payload(
                reply_type="list_reply", reply_id="int-3:0", title="Standard", description="3-5 business days"
            )
        )
    )

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Standard"
    assert call["params"] == {"reply_id": "int-3:0", "reply_description": "3-5 business days"}


async def test_button_message_type_bridges_text_and_payload(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A template quick-reply tap arrives as a ``button`` message (fields button.text +
    # button.payload); it bridges the text with params.button_payload.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "button",
        "button": {"text": "Confirm appointment", "payload": "CONFIRM_APPT_42"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Confirm appointment"
    assert call["params"] == {"button_payload": "CONFIRM_APPT_42"}
    assert _SEEN_KEY in fake_redis.store


async def test_button_message_type_answers_pending_like_text(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A ``button`` quick-reply while a question is pending answers with its visible text,
    # mirroring a typed reply — the ask path, no bridge, no params seam.
    await _seed_pending_select(options=["Confirm", "Cancel"])
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "button",
        "button": {"text": "Confirm", "payload": "CONFIRM_APPT_42"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == "Confirm"  # answered as a typed reply would
    assert stub_app.conversations.accept_calls == []  # the ask path, not the bridge


async def test_referral_fields_carried_as_params(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A click-to-WhatsApp / QR referral forwards its fields as opaque, prefixed params
    # on the bridged turn.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "text",
        "text": {"body": "hi from the ad"},
        "referral": {
            "source_url": "https://fb.me/ad/123",
            "source_id": "ad-123",
            "source_type": "ad",
            "ctwa_clid": "clid-abc",
            "headline": "50% off today",
            "body": "Tap to chat",
        },
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "hi from the ad"
    assert call["params"] == {
        "referral_source_url": "https://fb.me/ad/123",
        "referral_source_id": "ad-123",
        "referral_source_type": "ad",
        "referral_ctwa_clid": "clid-abc",
        "referral_headline": "50% off today",
        "referral_body": "Tap to chat",
    }


async def test_reply_context_carried_as_params(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A message quoting an earlier one carries context.id as params.context_message_id.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "text",
        "text": {"body": "re: that"},
        "context": {"from": "15551112222", "id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "re: that"
    assert call["params"] == {"context_message_id": "wamid.QUOTED"}


async def test_referral_and_context_merge_with_reply_id_on_a_tap(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The message-level referral/context params merge with an interactive tap's reply_id
    # on one bridged turn (the union of both key spaces).
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "int-1:0", "title": "Yes"}},
        "context": {"id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["params"] == {"reply_id": "int-1:0", "context_message_id": "wamid.QUOTED"}


async def test_oversized_param_value_is_dropped_not_5xx(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A referral field over the contract's per-value cap is dropped at extraction (never
    # truncated, never a 5xx), while the well-formed sibling fields still ride.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "text",
        "text": {"body": "hi"},
        "referral": {"source_id": "ad-1", "headline": "x" * 600},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["params"] == {"referral_source_id": "ad-1"}  # the 600-char headline dropped
