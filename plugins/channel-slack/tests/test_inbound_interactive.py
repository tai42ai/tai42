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

from tai42_channel_slack.correlation import store_form_record
from tai42_channel_slack.forms import (
    FIELD_ACTION_ID,
    FORM_OPEN_ACTION_ID,
    FORM_SUBMIT_CALLBACK_ID,
    build_modal_view,
)
from tai42_channel_slack.inbound import slack_interactive
from tests.conftest import (
    TEST_BOT_TOKEN,
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


async def test_view_submission_forwards_coerced_answer_and_closes(fake_redis, http_script):
    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(200))

    response = await slack_interactive(_signed(_view_submission()))

    assert response.status_code == 200
    assert body_json(response) == {}  # empty body closes the modal
    (forward,) = http_script.requests
    assert str(forward.url) == _CALLBACK
    assert json.loads(forward.content) == {"answer": {"full_name": "Alice", "count": 3}}
    assert _FORM_KEY not in fake_redis.store  # dropped after the forward


async def test_view_submission_door_400_shows_error_keeps_record(fake_redis, http_script):
    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(400, json={"error": "full_name is required"}))

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {
        "response_action": "errors",
        "errors": {"full_name": "full_name is required"},
    }
    assert fake_redis.store[_FORM_KEY]  # kept: the human can correct and resubmit


async def test_view_submission_door_400_pins_named_field_block(fake_redis, http_script):
    # The door names the SECOND field: the error pins under its block_id, not the
    # first field's, so the human sees it on the control that failed.
    await _seed_form(fake_redis)
    http_script.results.append(
        httpx.Response(400, json={"error": "answer does not match schema at count: ...", "field": "count"})
    )

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {
        "response_action": "errors",
        "errors": {"count": "answer does not match schema at count: ..."},
    }
    assert fake_redis.store[_FORM_KEY]


async def test_view_submission_door_400_unknown_field_falls_back_to_first(fake_redis, http_script):
    # A ``field`` that is not a declared schema property (or a nested path) has no
    # matching block_id: the error pins under the first field as before.
    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(400, json={"error": "bad", "field": "not_a_field"}))

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": "bad"}}
    assert fake_redis.store[_FORM_KEY]


async def test_view_submission_door_404_shows_expired_drops_record(fake_redis, http_script):
    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(404))

    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _EXPIRED_TEXT}}
    assert _FORM_KEY not in fake_redis.store


async def test_view_submission_door_500_raises(fake_redis, http_script):
    await _seed_form(fake_redis)
    http_script.results.append(httpx.Response(500))

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await slack_interactive(_signed(_view_submission()))

    assert fake_redis.store[_FORM_KEY]  # kept for Slack's implicit resubmit


async def test_view_submission_missing_record_shows_expired(fake_redis, http_script):
    # The form record lapsed between opening the modal and submitting it.
    response = await slack_interactive(_signed(_view_submission()))

    assert body_json(response) == {"response_action": "errors", "errors": {"full_name": _EXPIRED_TEXT}}
    assert http_script.requests == []


async def test_view_submission_missing_record_and_empty_state_raises(fake_redis):
    # No record and no input block to pin the error on: malformed, raised loudly.
    with pytest.raises(ValueError, match="no input block"):
        await slack_interactive(_signed(_view_submission(state={})))


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(400, text="boom"), id="non-json-body"),
        pytest.param(httpx.Response(400, json={"detail": "nope"}), id="json-without-error"),
    ],
)
async def test_view_submission_door_400_without_usable_error_uses_fallback(fake_redis, http_script, response):
    await _seed_form(fake_redis)
    http_script.results.append(response)

    result = await slack_interactive(_signed(_view_submission()))

    assert body_json(result) == {
        "response_action": "errors",
        "errors": {"full_name": "The form could not be submitted."},
    }
    assert fake_redis.store[_FORM_KEY]


async def test_view_submission_wrong_callback_id_is_ignored(fake_redis, http_script):
    response = await slack_interactive(_signed(_view_submission(callback_id="not_ours")))

    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []


async def test_view_submission_without_private_metadata_raises(fake_redis):
    with pytest.raises(ValueError, match="private_metadata"):
        await slack_interactive(_signed(_view_submission(private_metadata="")))
