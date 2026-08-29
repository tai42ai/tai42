"""The interactivity door: signature auth, the ``block_actions`` modal open, and
the ``view_submission`` forward with its door-status policy. Every request is
signed with the test secret over the raw FORM-ENCODED body (``payload=<json>``),
so the flow always crosses real verification first."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome

from tai42_channel_slack.blocks import SELECT_ACTION_PREFIX
from tai42_channel_slack.correlation import store_correlation, store_form_record
from tai42_channel_slack.forms import (
    FIELD_ACTION_ID,
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    build_modal_view,
)
from tai42_channel_slack.inbound import _MODAL_RETRY_TEXT as _RETRY_TEXT
from tai42_channel_slack.inbound import slack_interactive

from .conftest import (
    TEST_BOT_TOKEN,
    TEST_DEFAULT_RECIPIENT,
    TEST_SIGNING_SECRET,
    body_json,
    make_interactive_body,
    make_request,
    signed_headers,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_INTERACTION_ID = "int-77"
_CALLBACK = "http://gateway/api/interactions/callback/ticket-form"
_QUESTION = "Give us your details"
_FORM_KEY = f"channel:slack:form:{_INTERACTION_ID}"
_EXPIRED_TEXT = "This question is no longer available; it has expired."

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string", "title": "Full name"},
        "count": {"type": "integer"},
    },
    "required": ["full_name"],
}


def _signed(payload: dict[str, Any]):
    body = make_interactive_body(payload)
    return make_request(body, signed_headers(body, TEST_SIGNING_SECRET))


async def _seed_form(fake_redis) -> None:
    await store_form_record(_INTERACTION_ID, _CALLBACK, _SCHEMA, _QUESTION, datetime.now(UTC) + timedelta(minutes=10))


def _block_actions(action_id: str = FORM_OPEN_ACTION_ID, value: Any = _INTERACTION_ID, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "block_actions", "trigger_id": "trg-1", "actions": [{"action_id": action_id}]}
    if value is not None:
        payload["actions"][0]["value"] = value
    payload.update(extra)
    return payload


def _default_state() -> dict[str, Any]:
    return {
        "full_name": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "Alice"}},
        "count": {FIELD_ACTION_ID: {"type": "number_input", "value": "3"}},
    }


def _view_submission(state: dict[str, Any] | None = None, **view_overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "callback_id": FORM_SUBMIT_CALLBACK_ID,
        "private_metadata": _INTERACTION_ID,
        "state": {"values": state if state is not None else _default_state()},
    }
    view.update(view_overrides)
    return {"type": "view_submission", "view": view}


# -- transport auth -----------------------------------------------------------


async def test_unsigned_request_is_401(fake_redis):
    response = await slack_interactive(make_request(make_interactive_body(_block_actions()), {}))
    assert response.status_code == 401
    assert body_json(response) == {"error": "signature verification failed"}


async def test_unset_signing_secret_is_500(no_slack_env):
    response = await slack_interactive(_signed(_block_actions()))
    assert response.status_code == 500
    assert body_json(response) == {"error": "channel misconfigured"}


async def test_oversized_body_is_413():
    response = await slack_interactive(make_request(b"x" * (1 * 1024 * 1024 + 1), {}))
    assert response.status_code == 413


async def test_body_without_payload_field_is_400():
    body = b"nothing=here"
    response = await slack_interactive(make_request(body, signed_headers(body, TEST_SIGNING_SECRET)))
    assert response.status_code == 400
    assert body_json(response) == {"error": "body must carry a JSON payload field"}


async def test_unknown_payload_type_is_acked_ignored():
    response = await slack_interactive(_signed({"type": "shortcut"}))
    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}


# -- option-button taps (select / suggested-reply / notify options) -----------


def _select_tap(
    index: int = 1,
    value: Any = "blue",
    message_ts: str = "123.456",
    channel_id: str = TEST_DEFAULT_RECIPIENT,
    action_ts: str = "1700.1",
) -> dict[str, Any]:
    action: dict[str, Any] = {"action_id": f"{SELECT_ACTION_PREFIX}{index}", "type": "button", "action_ts": action_ts}
    if value is not None:
        action["value"] = value
    return {
        "type": "block_actions",
        "user": {"id": "U0GUEST"},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel_id},
        "channel": {"id": channel_id, "name": "chan"},
        "actions": [action],
    }


async def test_select_button_tap_resolves_via_ladder(fake_redis, channels, stub_conversations):
    # A tap on a select option button resolves through the ladder with the anchor
    # message ts as the correlation key and the button value as the answer.
    await store_correlation("123.456", _CALLBACK, "int-1", datetime.now(UTC) + timedelta(minutes=10))
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await slack_interactive(_signed(_select_tap(index=1, value="blue")))

    assert response.status_code == 200
    assert body_json(response) == {"status": "forwarded"}
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.correlation_key == "123.456"
    assert call.answer == "blue"
    assert call.bridge.provider_message_id == "1700.1"  # the tap's action_ts


async def test_notify_option_tap_bridges_on_correlation_miss(fake_redis, channels, stub_conversations):
    # A notify-option tap has no pending ask: the thread peek misses, so the option
    # text enters the conversation as a visitor message (no ladder call).
    response = await slack_interactive(_signed(_select_tap(index=0, value="apples")))

    assert response.status_code == 200
    assert body_json(response) == {"status": "accepted"}
    assert channels.inbound_calls == []
    assert len(stub_conversations.accept_calls) == 1
    call = stub_conversations.accept_calls[0]
    assert call.text == "apples"
    assert call.client_address == TEST_DEFAULT_RECIPIENT


async def test_tap_from_non_allowlisted_channel_bridges_without_resolving(fake_redis, channels, stub_conversations):
    # A live ask is anchored on this ts, but the tap arrives from a channel OUTSIDE the
    # allowlist. Mirroring the typed-reply path's gate (_process_event), the tap never
    # reaches the answer ladder — it bridges directly, so a tap from a non-allowlisted
    # channel can never resolve a pending ask, exactly as a typed message would not.
    await store_correlation("123.456", _CALLBACK, "int-1", datetime.now(UTC) + timedelta(minutes=10))
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await slack_interactive(_signed(_select_tap(index=1, value="blue", channel_id="C0OUTSIDE")))

    assert response.status_code == 200
    assert body_json(response) == {"status": "accepted"}
    assert channels.inbound_calls == []  # the ladder was never consulted
    assert len(stub_conversations.accept_calls) == 1
    call = stub_conversations.accept_calls[0]
    assert call.text == "blue"
    assert call.client_address == "C0OUTSIDE"


async def test_select_tap_without_value_is_acked_ignored(fake_redis, channels, stub_conversations):
    response = await slack_interactive(_signed(_select_tap(value=None)))
    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}
    assert channels.inbound_calls == []
    assert stub_conversations.accept_calls == []


async def test_select_tap_without_anchor_ts_is_acked_ignored(fake_redis, channels, stub_conversations):
    payload = _select_tap()
    payload["container"] = {"type": "message"}  # no message_ts
    response = await slack_interactive(_signed(payload))
    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}
    assert channels.inbound_calls == []


async def test_non_utf8_body_is_400():
    body = b"\x80\x81\x82"
    response = await slack_interactive(make_request(body, signed_headers(body, TEST_SIGNING_SECRET)))
    assert response.status_code == 400


async def test_payload_field_not_json_is_400():
    body = urlencode({"payload": "not-json"}).encode()
    response = await slack_interactive(make_request(body, signed_headers(body, TEST_SIGNING_SECRET)))
    assert response.status_code == 400


# -- block_actions ------------------------------------------------------------


async def test_block_actions_opens_modal_with_exact_payload(fake_redis, http_script):
    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(200, json={"ok": True}))

    response = await slack_interactive(_signed(_block_actions()))

    assert body_json(response) == {"status": "opened"}
    (req,) = http_script.requests
    assert str(req.url) == "https://slack.com/api/views.open"
    assert req.headers["Authorization"] == f"Bearer {TEST_BOT_TOKEN}"
    assert json.loads(req.content) == {
        "trigger_id": "trg-1",
        "view": build_modal_view(_INTERACTION_ID, _QUESTION, _SCHEMA),
    }


async def test_block_actions_missing_record_acks_without_opening(fake_redis, http_script):
    # The button outlived its question (record expired) — nothing to open.
    response = await slack_interactive(_signed(_block_actions()))

    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []


async def test_block_actions_unknown_action_is_ignored(fake_redis, http_script):
    await _seed_form(fake_redis)

    response = await slack_interactive(_signed(_block_actions(action_id="some_other_button")))

    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []


async def test_block_actions_without_value_raises(fake_redis):
    with pytest.raises(ValueError, match="interaction id"):
        await slack_interactive(_signed(_block_actions(value=None)))


async def test_block_actions_without_trigger_id_raises(fake_redis):
    await _seed_form(fake_redis)
    payload = _block_actions()
    del payload["trigger_id"]

    with pytest.raises(ValueError, match="trigger_id"):
        await slack_interactive(_signed(payload))


async def test_failed_views_open_surfaces_loudly(fake_redis, http_script):
    from tai42_contract.channels import ChannelDeliveryError

    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(200, json={"ok": False, "error": "expired_trigger_id"}))

    with pytest.raises(ChannelDeliveryError, match="expired_trigger_id"):
        await slack_interactive(_signed(_block_actions()))


# -- view_submission ----------------------------------------------------------


async def test_view_submission_forwards_coerced_answer_and_closes(fake_redis, channels):
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    response = await slack_interactive(_signed(_view_submission()))

    assert response.status_code == 200
    assert body_json(response) == {}  # empty body closes the modal
    (call,) = channels.inbound_calls
    assert call.correlation_key == _INTERACTION_ID
    assert call.answer == {"full_name": "Alice", "count": 3}  # coerced per the schema
    # A completed modal has no re-reply surface — the channel owns the retry notice.
    assert call.bridge.owns_retry_notice is True
    assert _FORM_KEY not in fake_redis.store  # released by the ladder (mirrored)


async def test_view_submission_retry_kept_shows_door_reason_keeps_record(fake_redis, channels):
    # RETRY_KEPT: the ladder kept the record and sent NO guest notice (owns_retry_notice
    # =True), so the channel renders its own inline Block-Kit error carrying the DOOR'S
    # specific reason and the modal stays open — one guest surface, no double message.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "full_name is required"

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {
        "response_action": "errors",
        "errors": {"full_name": "full_name is required"},  # the door's own message, not generic text
    }
    assert fake_redis.store[_FORM_KEY]  # kept: the human can correct and resubmit


async def test_view_submission_retry_kept_pins_door_named_field_block(fake_redis, channels):
    # The door names the SECOND field: the error pins under its block_id, not the first
    # field's, so the human sees it on the control that failed (restored fidelity).
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "answer does not match schema at count: ..."
    channels.inbound_retry_field = "count"

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {
        "response_action": "errors",
        "errors": {"count": "answer does not match schema at count: ..."},
    }
    assert fake_redis.store[_FORM_KEY]


async def test_view_submission_retry_kept_unknown_field_falls_back_to_first(fake_redis, channels):
    # A door ``field`` that is not a declared schema property has no matching block_id:
    # the error pins under the first field.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "bad"
    channels.inbound_retry_field = "not_a_field"

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": "bad"}}


async def test_view_submission_retry_kept_without_reason_uses_generic_text(fake_redis, channels):
    # When the door gave no usable reason, the inline error falls back to generic text
    # under the first field.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = None

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _RETRY_TEXT}}
    assert fake_redis.store[_FORM_KEY]


async def test_view_submission_bridged_shows_expired_drops_record(fake_redis, channels):
    # BRIDGED: the ask is gone (a 404) — the ladder released the record and bridged the
    # submission; the modal shows the expired notice.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _EXPIRED_TEXT}}
    assert _FORM_KEY not in fake_redis.store  # released by the ladder (mirrored)


async def test_view_submission_no_correlation_shows_expired(fake_redis, channels):
    # The record lapsed between the channel's read and the ladder's peek: NO_CORRELATION
    # — the modal shows the expired notice.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _EXPIRED_TEXT}}


async def test_view_submission_forward_error_raises(fake_redis, channels):
    await _seed_form(fake_redis)
    channels.inbound_error = AnswerForwardError("callback forward failed: HTTP 500 from the interactions door")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await slack_interactive(_signed(_view_submission()))

    assert fake_redis.store[_FORM_KEY]  # kept for Slack's implicit resubmit


async def test_view_submission_missing_record_shows_expired(fake_redis, channels):
    # The form record lapsed between opening the modal and submitting it — the channel
    # never reaches the ladder.
    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _EXPIRED_TEXT}}
    assert channels.inbound_calls == []


async def test_view_submission_missing_record_and_empty_state_raises(fake_redis):
    # No record and no input block to pin the error on: malformed, raised loudly.
    with pytest.raises(ValueError, match="no input block"):
        await slack_interactive(_signed(_view_submission(state={})))


async def test_view_submission_wrong_callback_id_is_ignored(fake_redis, http_script):
    response = await slack_interactive(_signed(_view_submission(callback_id="not_ours")))

    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []


async def test_view_submission_without_private_metadata_raises(fake_redis):
    with pytest.raises(ValueError, match="private_metadata"):
        await slack_interactive(_signed(_view_submission(private_metadata="")))
