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

from tai42_channel_slack.blocks import REPLY_ACTION_PREFIX, SELECT_ACTION_PREFIX, encode_reply_value
from tai42_channel_slack.correlation import store_correlation, store_form_record
from tai42_channel_slack.forms import (
    FIELD_ACTION_ID,
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    build_modal_view,
    decode_private_metadata,
)
from tai42_channel_slack.inbound.interactive import _MODAL_RETRY_TEXT as _RETRY_TEXT
from tai42_channel_slack.inbound.interactive import _REACTION_FAILURE_TEXT, slack_interactive

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
        "private_metadata": json.dumps({"id": _INTERACTION_ID}),
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
        "user": {"id": "U0PARTICIPANT"},
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


def _reply_tap(
    index: int = 0,
    text: str = "apples",
    option_id: str | None = None,
    message_ts: str = "123.456",
    channel_id: str = TEST_DEFAULT_RECIPIENT,
    action_ts: str = "1700.1",
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": f"{REPLY_ACTION_PREFIX}{index}",
        "type": "button",
        "action_ts": action_ts,
        "value": encode_reply_value(text, option_id),
    }
    return {
        "type": "block_actions",
        "user": {"id": "U0PARTICIPANT"},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel_id},
        "channel": {"id": channel_id, "name": "chan"},
        "actions": [action],
    }


async def test_reply_option_tap_bridges_with_reply_id_param(fake_redis, channels, stub_conversations):
    # A notify reply-option tap carries the submit text plus the author-set id in its value
    # envelope. With no pending ask it bridges: the text is the turn, and the id rides as
    # params.reply_id (the channel-agnostic tap enrichment a consumer reads).
    response = await slack_interactive(_signed(_reply_tap(text="Retry it", option_id="opt-retry")))

    assert response.status_code == 200
    assert body_json(response) == {"status": "accepted"}
    assert channels.inbound_calls == []
    (call,) = stub_conversations.accept_calls
    assert call.text == "Retry it"
    assert call.params == {"reply_id": "opt-retry"}


async def test_reply_option_tap_without_id_bridges_without_params(fake_redis, channels, stub_conversations):
    # A reply option with no author-set id bridges the text with no enrichment params.
    response = await slack_interactive(_signed(_reply_tap(text="Just this", option_id=None)))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.text == "Just this"
    assert call.params is None


async def test_reply_option_tap_forwards_reply_id_on_the_answer_path(fake_redis, channels, stub_conversations):
    # A reply-option tap that lands on a live ask resolves through the ladder; the author-set
    # id rides the InboundBridge.params the ladder threads to the callback and the turn.
    await store_correlation("123.456", _CALLBACK, "int-1", datetime.now(UTC) + timedelta(minutes=10))
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await slack_interactive(_signed(_reply_tap(text="Yes", option_id="opt-yes")))

    assert body_json(response) == {"status": "forwarded"}
    (call,) = channels.inbound_calls
    assert call.answer == "Yes"
    assert call.bridge.params == {"reply_id": "opt-yes"}


async def test_link_option_tap_is_acked_ignored(fake_redis, channels, stub_conversations):
    # A link (url) button tap opens the url and submits nothing, so the door acks and ignores
    # it — never a bridged turn, never an answer.
    tap = _reply_tap()
    tap["actions"][0]["action_id"] = "tai42_link:0"
    tap["actions"][0]["url"] = "https://app.test/dash"
    response = await slack_interactive(_signed(tap))

    assert body_json(response) == {"status": "ignored"}
    assert channels.inbound_calls == []
    assert stub_conversations.accept_calls == []


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


async def test_block_actions_modal_carries_the_reserved_per_send_data_and_pages(fake_redis, http_script):
    # The modal is built AT CLICK TIME from the reserved record, so the per-send
    # values/options and step layout reach the opened view.
    schema = {
        "type": "object",
        "properties": {"full_name": {"type": "string", "title": "Full name"}, "tier": {"type": "string"}},
    }
    data = {"values": {"full_name": "Ada"}, "options": {"tier": [{"value": "g", "label": "Gold"}]}}
    pages = [{"title": "You", "fields": ["full_name"]}, {"title": "Plan", "fields": ["tier"]}]
    await store_form_record(
        _INTERACTION_ID,
        _CALLBACK,
        schema,
        _QUESTION,
        datetime.now(UTC) + timedelta(minutes=10),
        data=data,
        pages=pages,
    )
    http_script.results.append(httpx.Response(200, json={"ok": True}))

    await slack_interactive(_signed(_block_actions()))

    (req,) = http_script.requests
    view = json.loads(req.content)["view"]
    assert view == build_modal_view(_INTERACTION_ID, _QUESTION, schema, data["values"], data["options"], pages)
    # A concrete assertion the built view reflects the enrichment: the prefill, the
    # per-send select, and the page headers are all present.
    by_id = {b.get("block_id"): b for b in view["blocks"] if b["type"] == "input"}
    assert by_id["full_name"]["element"]["initial_value"] == "Ada"
    assert by_id["tier"]["element"]["options"] == [{"text": {"type": "plain_text", "text": "Gold"}, "value": "g"}]
    assert [b["text"]["text"] for b in view["blocks"] if b["type"] == "header"] == ["You", "Plan"]


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


async def test_failed_views_open_surfaces_every_documented_field(fake_redis, http_script):
    # views.open routes its ok:false body through the SAME _error_detail helper as
    # chat.postMessage: every documented field kept in Slack's fixed order.
    from tai42_contract.channels import ChannelDeliveryError

    await _seed_form(fake_redis)
    body = {
        "ok": False,
        "error": "invalid_arguments",
        "warning": "missing_charset",
        "needed": "chat:write",
        "provided": "chat:read",
        "response_metadata": {"messages": ["[ERROR] invalid view"]},
    }
    http_script.results.append(httpx.Response(200, json=body))

    with pytest.raises(ChannelDeliveryError) as excinfo:
        await slack_interactive(_signed(_block_actions()))

    assert (
        "views.open failed: error='invalid_arguments' warning='missing_charset' "
        "needed='chat:write' provided='chat:read' "
        'response_metadata={"messages":["[ERROR] invalid view"]}'
    ) in str(excinfo.value)


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
    # RETRY_KEPT: the ladder kept the record and sent NO participant notice (owns_retry_notice
    # =True), so the channel renders its own inline Block-Kit error carrying the DOOR'S
    # specific reason and the modal stays open — one participant surface, no double message.
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
    # field's, so the human sees it on the control that failed.
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


async def test_view_submission_bridged_kept_closes_without_expired_notice(fake_redis, channels):
    # BRIDGED_KEPT: a bridge-policy ask KEPT the correlation (still parked) and bridged the
    # submission as a fresh turn — a digression, not the answer. The modal closes with an
    # empty body (the reply was consumed as a turn) and is NEVER told it expired; the parked
    # ask stays answerable.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED_KEPT

    response = await slack_interactive(_signed(_view_submission()))

    assert response.status_code == 200
    assert body_json(response) == {}  # empty body closes the modal — no expired notice
    assert _EXPIRED_TEXT not in json.dumps(body_json(response))
    assert fake_redis.store[_FORM_KEY]  # the ask stays parked (KEPT), still answerable


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


# -- B/A: a field change re-renders the modal (conditional) or runs a reaction ----------------


_FUTURE = datetime.now(UTC) + timedelta(minutes=10)


def _field_action(block_id: str, state: dict[str, Any], private_metadata: str | None = None) -> dict[str, Any]:
    """A ``block_actions`` from a modal field change: the changed block, the view's state/id/hash."""
    return {
        "type": "block_actions",
        "actions": [{"action_id": FIELD_ACTION_ID, "block_id": block_id}],
        "view": {
            "id": "V1",
            "hash": "h1",
            "private_metadata": private_metadata
            if private_metadata is not None
            else json.dumps({"id": _INTERACTION_ID}),
            "state": {"values": state},
        },
    }


_COND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["a", "b"]},
        "detail": {"type": "string", "visibleWhen": {"field": "kind", "equals": "a"}},
    },
}


async def test_field_change_re_renders_conditional_without_a_consumer_call(fake_redis, http_script, interactions):
    # Conditional visibility on Slack: the platform re-evaluates visibleWhen HERE and views.update the
    # modal to hide the field — never a call to the consumer's reaction handler.
    await store_form_record(_INTERACTION_ID, _CALLBACK, _COND_SCHEMA, _QUESTION, _FUTURE)
    http_script.results.append(httpx.Response(200, json={"ok": True}))
    state = {"kind": {FIELD_ACTION_ID: {"type": "radio_buttons", "selected_option": {"value": "b"}}}}

    response = await slack_interactive(_signed(_field_action("kind", state)))

    assert body_json(response) == {"status": "updated"}
    assert interactions.react_calls == []  # platform-evaluated, no consumer reaction
    (req,) = http_script.requests
    assert str(req.url) == "https://slack.com/api/views.update"
    payload = json.loads(req.content)
    assert payload["view_id"] == "V1"
    assert payload["hash"] == "h1"
    ids = [b.get("block_id") for b in payload["view"]["blocks"] if b["type"] == "input"]
    assert "detail" not in ids  # hidden: kind == "b"
    assert "kind" in ids


async def test_field_change_failed_views_update_surfaces_loudly(fake_redis, http_script, interactions):
    from tai42_contract.channels import ChannelDeliveryError

    await store_form_record(_INTERACTION_ID, _CALLBACK, _COND_SCHEMA, _QUESTION, _FUTURE)
    http_script.results.append(httpx.Response(200, json={"ok": False, "error": "not_found"}))
    state = {"kind": {FIELD_ACTION_ID: {"type": "radio_buttons", "selected_option": {"value": "b"}}}}

    with pytest.raises(ChannelDeliveryError, match="not_found"):
        await slack_interactive(_signed(_field_action("kind", state)))


async def test_field_change_runs_reaction_and_applies_the_update(fake_redis, http_script, interactions):
    # A field_changed reaction runs through the ONE chokepoint and its update is
    # applied by views.update — values set, option lists replaced, display slots filled, errors shown.
    schema = {
        "type": "object",
        "properties": {"qty": {"type": "integer"}, "plan": {"type": "string", "enum": ["basic"]}},
    }
    pages = [
        {"title": "Details", "fields": ["qty", "plan"], "kind": "input", "display": [{"kind": "body", "slot": "total"}]}
    ]
    reactions = {"field_changed": ["qty"], "page_advanced": [], "submitted": False, "choices": ["plan"]}
    await store_form_record(_INTERACTION_ID, _CALLBACK, schema, _QUESTION, _FUTURE, pages=pages, reactions=reactions)
    interactions.react_result = {
        "values": {"qty": 2},
        "options": {"plan": [{"value": "basic", "label": "Basic"}, {"value": "pro", "label": "Pro"}]},
        "errors": {"qty": "check qty"},
        "display": {"total": "Total: 20"},
    }
    http_script.results.append(httpx.Response(200, json={"ok": True}))
    state = {"qty": {FIELD_ACTION_ID: {"type": "number_input", "value": "1"}}}

    response = await slack_interactive(_signed(_field_action("qty", state)))

    assert body_json(response) == {"status": "updated"}
    (call,) = interactions.react_calls
    assert call.interaction_id == _INTERACTION_ID
    assert call.event == {"kind": "field_changed", "field": "qty"}
    assert call.partial_values == {"qty": 1}  # the values filled so far, before the update
    view = json.loads(http_script.requests[0].content)["view"]
    by_id = {b["block_id"]: b for b in view["blocks"] if b["type"] == "input"}
    assert by_id["qty"]["element"]["initial_value"] == "2"  # reaction set value
    assert [o["value"] for o in by_id["plan"]["element"]["options"]] == ["basic", "pro"]  # list replaced
    assert any(b["type"] == "section" and b.get("text", {}).get("text") == "Total: 20" for b in view["blocks"])
    assert any(b["type"] == "context" and "check qty" in b["elements"][0]["text"] for b in view["blocks"])
    # The accumulated reaction state rides private_metadata, surviving the next re-render.
    _id, opts, disp = decode_private_metadata(view["private_metadata"])
    assert opts["plan"] == [{"value": "basic", "label": "Basic"}, {"value": "pro", "label": "Pro"}]
    assert disp == {"total": "Total: 20"}


async def test_field_change_reaction_failure_shows_a_banner(fake_redis, http_script, interactions):
    # A handler failure surfaces LOUDLY — a banner notice on the re-rendered modal — never a
    # stale or silent value.
    schema = {"type": "object", "properties": {"qty": {"type": "integer"}}}
    reactions = {"field_changed": ["qty"], "page_advanced": [], "submitted": False, "choices": []}
    await store_form_record(_INTERACTION_ID, _CALLBACK, schema, _QUESTION, _FUTURE, reactions=reactions)
    interactions.react_error = RuntimeError("boom")
    http_script.results.append(httpx.Response(200, json={"ok": True}))
    state = {"qty": {FIELD_ACTION_ID: {"type": "number_input", "value": "1"}}}

    response = await slack_interactive(_signed(_field_action("qty", state)))

    assert body_json(response) == {"status": "updated"}
    assert len(interactions.react_calls) == 1
    view = json.loads(http_script.requests[0].content)["view"]
    assert view["blocks"][1]["type"] == "section"  # banner right after the question section
    assert "could not be updated" in view["blocks"][1]["text"]["text"]


async def test_field_change_with_no_record_is_ignored(fake_redis, http_script, interactions):
    # The modal outlived its question: nothing to re-render, no reaction run, no update sent.
    state = {"kind": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "x"}}}
    response = await slack_interactive(_signed(_field_action("kind", state)))

    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []
    assert interactions.react_calls == []


async def test_field_change_on_unstamped_view_is_ignored(fake_redis, http_script, interactions):
    await _seed_form(fake_redis)
    state = {"full_name": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "x"}}}
    response = await slack_interactive(_signed(_field_action("full_name", state, private_metadata="not-json")))

    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []


# -- A: the submitted reaction is the consumer's final check on view_submission ---------------


async def _seed_reacting_submit(fake_redis) -> None:
    reactions = {"field_changed": [], "page_advanced": [], "submitted": True, "choices": []}
    await store_form_record(_INTERACTION_ID, _CALLBACK, _SCHEMA, _QUESTION, _FUTURE, reactions=reactions)


async def test_view_submission_date_bound_violation_degrades_to_an_inline_error(fake_redis, channels):
    # Date-bound degrade on Slack: the date picker draws no bound, so an out-of-range date
    # reaches submit, the one answer check rejects it (AnswerMismatchError field="on"), and the
    # ladder's RETRY_KEPT carries that field — the door pins it as a response_action:errors on
    # the date field's own block_id (the modal stays open to correct it).
    schema = {
        "type": "object",
        "properties": {"on": {"type": "string", "format": "date", "minDate": "2026-01-01", "maxDate": "2026-12-31"}},
    }
    await store_form_record(_INTERACTION_ID, _CALLBACK, schema, _QUESTION, _FUTURE)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "answer does not match schema at on: date before minDate"
    channels.inbound_retry_field = "on"
    state = {"on": {FIELD_ACTION_ID: {"type": "datepicker", "selected_date": "2025-06-01"}}}

    response = await slack_interactive(_signed(_view_submission(state=state)))

    assert body_json(response) == {
        "response_action": "errors",
        "errors": {"on": "answer does not match schema at on: date before minDate"},
    }
    assert fake_redis.store[_FORM_KEY]  # kept: the human corrects the out-of-range date and resubmits


async def test_view_submission_submitted_reaction_errors_keep_the_modal_open(fake_redis, channels, interactions):
    await _seed_reacting_submit(fake_redis)
    interactions.react_result = {"errors": {"full_name": "not allowed"}}

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": "not allowed"}}
    (call,) = interactions.react_calls
    assert call.event == {"kind": "submitted"}
    assert channels.inbound_calls == []  # the answer was NOT forwarded — the consumer refused it


async def test_view_submission_submitted_reaction_clean_forwards_the_answer(fake_redis, channels, interactions):
    await _seed_reacting_submit(fake_redis)
    interactions.react_result = {}  # the consumer accepts
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {}  # forwarded closes the modal
    (call,) = interactions.react_calls
    assert call.event == {"kind": "submitted"}
    assert call.partial_values == {"full_name": "Alice", "count": 3}
    (forwarded,) = channels.inbound_calls
    assert forwarded.answer == {"full_name": "Alice", "count": 3}


async def test_view_submission_submitted_reaction_finalizes_values(fake_redis, channels, interactions):
    await _seed_reacting_submit(fake_redis)
    interactions.react_result = {"values": {"count": 99}}  # the check normalises a value
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {}
    (forwarded,) = channels.inbound_calls
    assert forwarded.answer["count"] == 99  # the finalized value is what the consumer receives


async def test_view_submission_submitted_reaction_failure_is_a_loud_inline_error(fake_redis, channels, interactions):
    await _seed_reacting_submit(fake_redis)
    interactions.react_error = RuntimeError("boom")

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _REACTION_FAILURE_TEXT}}
    assert channels.inbound_calls == []  # never silently accepted


async def test_view_submission_without_reactions_runs_no_submitted_check(fake_redis, channels, interactions):
    # A static form reaches the ladder directly — the reaction chokepoint is never consulted.
    await _seed_form(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    await slack_interactive(_signed(_view_submission()))

    assert interactions.react_calls == []
