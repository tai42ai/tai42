"""The quoted message's id: a thread reply's parent ``ts`` and a tapped message's ``ts`` reach
the platform as ``reply_to_medium_message_id`` on every arm — the inbound-answer ladder's
bridge and the conversation bridge's ``accept``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.channels import InboundAnswerOutcome
from tai42_contract.interactions.models import MediaKind

from tai42_channel_slack.blocks import SELECT_ACTION_PREFIX
from tai42_channel_slack.correlation import store_correlation, store_form_record
from tai42_channel_slack.forms import FIELD_ACTION_ID, FORM_SUBMIT_CALLBACK_ID
from tai42_channel_slack.inbound import events
from tai42_channel_slack.inbound.events import slack_inbound
from tai42_channel_slack.inbound.interactive import slack_interactive

from .conftest import (
    TEST_DEFAULT_RECIPIENT,
    TEST_SIGNING_SECRET,
    body_json,
    fake_open_media_stream,
    make_ingested,
    make_interactive_body,
    make_request,
    signed_headers,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_PARENT_TS = "1712345678.000100"
_OWN_TS = "1712345679.000200"
_CALLBACK = "http://gateway/api/interactions/callback/ticket-7"
_LADDER_ARMS = [InboundAnswerOutcome.FORWARDED, InboundAnswerOutcome.BRIDGED]


def _signed_event(event: dict[str, Any]):
    body = json.dumps({"type": "event_callback", "event_id": "Ev001", "event": event}).encode()
    return make_request(body, signed_headers(body, TEST_SIGNING_SECRET))


def _signed_interactive(payload: dict[str, Any]):
    body = make_interactive_body(payload)
    return make_request(body, signed_headers(body, TEST_SIGNING_SECRET))


def _message(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": TEST_DEFAULT_RECIPIENT,
        "text": "yes, deploy it",
        "user": "U012345",
        "ts": _OWN_TS,
    }
    event.update(overrides)
    return event


def _thread_reply(**overrides: Any) -> dict[str, Any]:
    return _message(thread_ts=_PARENT_TS, **overrides)


def _seed_correlation(fake_redis, thread_ts: str = _PARENT_TS) -> None:
    key = f"channel:slack:corr:{thread_ts}"
    fake_redis.store[key] = json.dumps(
        {
            "callback_url": _CALLBACK,
            "interaction_id": "int-7",
            "timeout_at": "2999-01-01T00:00:00+00:00",
            "on_mismatch": "retry",
            "wording": None,
        }
    )
    fake_redis.ttls[key] = 300


# --- a typed message ----------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", _LADDER_ARMS)
async def test_a_thread_reply_answering_an_ask_hands_the_ladder_bridge_the_parent_ts(fake_redis, channels, outcome):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = outcome

    await slack_inbound(_signed_event(_thread_reply()))

    (call,) = channels.inbound_calls
    assert call.bridge.medium_message_id == _OWN_TS
    assert call.bridge.reply_to_medium_message_id == _PARENT_TS


async def test_a_thread_reply_with_no_pending_ask_hands_accept_the_parent_ts(fake_redis, channels, stub_conversations):
    await slack_inbound(_signed_event(_thread_reply()))

    assert channels.inbound_calls == []
    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id == _PARENT_TS


async def test_a_thread_reply_whose_ask_is_gone_at_the_ladder_bridges_with_the_parent_ts(
    fake_redis, channels, stub_conversations
):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION

    await slack_inbound(_signed_event(_thread_reply()))

    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id == _PARENT_TS


async def test_a_thread_reply_outside_the_ask_channels_hands_accept_the_parent_ts(
    fake_redis, channels, stub_conversations
):
    _seed_correlation(fake_redis)

    await slack_inbound(_signed_event(_thread_reply(channel="C0OUTSIDE")))

    assert channels.inbound_calls == []
    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id == _PARENT_TS


async def test_a_thread_parent_quotes_nothing(fake_redis, channels, stub_conversations):
    # A thread's parent carries thread_ts == ts: it starts the thread, it quotes nothing.
    await slack_inbound(_signed_event(_message(thread_ts=_OWN_TS)))

    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id is None


@pytest.mark.parametrize("thread_ts", [None, "", 42], ids=["absent", "empty", "non_string"])
async def test_a_message_outside_any_thread_quotes_nothing(fake_redis, stub_conversations, thread_ts):
    event = _message() if thread_ts is None else _message(thread_ts=thread_ts)

    await slack_inbound(_signed_event(event))

    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id is None


# --- a file share ---------------------------------------------------------------------------------


def _file_share(files: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    return _message(subtype="file_share", files=files, **overrides)


async def test_every_file_of_a_threaded_file_share_hands_accept_the_parent_ts(
    fake_redis, stub_conversations, stub_media, monkeypatch
):
    # Patched on the module this file imported: a test elsewhere re-imports the package.
    monkeypatch.setattr(events, "open_media_stream", fake_open_media_stream())
    stub_media.ingest_results = [
        make_ingested(kind=MediaKind.DOCUMENT, mime="application/pdf", filename="a.pdf", size=10),
        make_ingested(kind=MediaKind.VIDEO, mime="video/mp4", size=20),
    ]
    event = _file_share(
        [
            {"id": "F1", "url_private": "https://files.slack.com/f1", "name": "a.pdf", "mimetype": "application/pdf"},
            {"id": "F2", "url_private": "https://files.slack.com/f2", "name": "b.mp4", "mimetype": "video/mp4"},
        ],
        thread_ts=_PARENT_TS,
    )

    response = await slack_inbound(_signed_event(event))

    assert body_json(response) == {"status": "accepted"}
    assert [call.reply_to_medium_message_id for call in stub_conversations.accept_calls] == [_PARENT_TS, _PARENT_TS]


async def test_a_file_share_outside_any_thread_quotes_nothing(fake_redis, stub_conversations, stub_media, monkeypatch):
    # Patched on the module this file imported: a test elsewhere re-imports the package.
    monkeypatch.setattr(events, "open_media_stream", fake_open_media_stream())
    stub_media.ingest_default = make_ingested(kind=MediaKind.IMAGE, mime="image/png")
    event = _file_share([{"id": "F1", "url_private": "https://files.slack.com/f1", "mimetype": "image/png"}])

    await slack_inbound(_signed_event(event))

    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id is None


async def test_the_caption_of_a_refused_threaded_file_share_hands_accept_the_parent_ts(fake_redis, stub_conversations):
    event = _file_share([{"id": "F1", "filetype": "binary"}], text="just a note", thread_ts=_PARENT_TS)

    await slack_inbound(_signed_event(event))

    assert len(stub_conversations.rejected_calls) == 1
    (call,) = stub_conversations.accept_calls
    assert call.text == "just a note"
    assert call.reply_to_medium_message_id == _PARENT_TS


# --- an option tap ----------------------------------------------------------------------------------


def _select_tap(message_ts: str = "171.1", channel_id: str = TEST_DEFAULT_RECIPIENT) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": "U0PARTICIPANT"},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel_id},
        "channel": {"id": channel_id, "name": "chan"},
        "actions": [
            {"action_id": f"{SELECT_ACTION_PREFIX}1", "type": "button", "action_ts": "1700.1", "value": "blue"}
        ],
    }


@pytest.mark.parametrize("outcome", _LADDER_ARMS)
async def test_a_tap_answering_an_ask_hands_the_ladder_bridge_the_tapped_message_ts(fake_redis, channels, outcome):
    await store_correlation("171.1", _CALLBACK, "int-1", datetime.now(UTC) + timedelta(minutes=10))
    channels.inbound_outcome = outcome

    await slack_interactive(_signed_interactive(_select_tap()))

    (call,) = channels.inbound_calls
    assert call.bridge.medium_message_id is None
    assert call.bridge.reply_to_medium_message_id == "171.1"


async def test_a_tap_with_no_pending_ask_hands_accept_the_tapped_message_ts(fake_redis, channels, stub_conversations):
    await slack_interactive(_signed_interactive(_select_tap()))

    assert channels.inbound_calls == []
    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id == "171.1"


async def test_a_tap_whose_ask_is_gone_at_the_ladder_bridges_with_the_tapped_message_ts(
    fake_redis, channels, stub_conversations
):
    await store_correlation("171.1", _CALLBACK, "int-1", datetime.now(UTC) + timedelta(minutes=10))
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION

    await slack_interactive(_signed_interactive(_select_tap()))

    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id == "171.1"


async def test_a_tap_outside_the_ask_channels_hands_accept_the_tapped_message_ts(
    fake_redis, channels, stub_conversations
):
    await store_correlation("171.1", _CALLBACK, "int-1", datetime.now(UTC) + timedelta(minutes=10))

    await slack_interactive(_signed_interactive(_select_tap(channel_id="C0OUTSIDE")))

    assert channels.inbound_calls == []
    (call,) = stub_conversations.accept_calls
    assert call.reply_to_medium_message_id == "171.1"


# --- a modal submission ------------------------------------------------------------------------------


async def test_a_modal_submission_quotes_nothing(fake_redis, channels):
    schema = {"type": "object", "properties": {"full_name": {"type": "string"}}, "required": ["full_name"]}
    await store_form_record("int-77", _CALLBACK, schema, "Your name?", datetime.now(UTC) + timedelta(minutes=10))
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    submission = {
        "type": "view_submission",
        "view": {
            "callback_id": FORM_SUBMIT_CALLBACK_ID,
            "private_metadata": json.dumps({"id": "int-77"}),
            "state": {"values": {"full_name": {FIELD_ACTION_ID: {"type": "plain_text_input", "value": "Alice"}}}},
        },
    }

    await slack_interactive(_signed_interactive(submission))

    (call,) = channels.inbound_calls
    assert call.bridge.reply_to_medium_message_id is None
