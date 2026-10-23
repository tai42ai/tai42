"""The send record on the conversation record model: ``origin="send"`` with its ``sent_by``, the two
send-only delivery states, the ask fields and the caller-safe view.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from tai42_skeleton.conversations.models import (
    ANSWERLESS_STATUSES,
    TERMINAL_STATUSES,
    AskReply,
    AskSend,
    ConversationRecord,
    DeliveryStatus,
    SentMessage,
)

_REPLY_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _send(**overrides) -> ConversationRecord:
    now = time.time()
    fields: dict = {
        "message_id": "s1",
        "route_name": "chat",
        "door": "channel",
        "thread_id": "bridge:chat:+1",
        "client_address": "+1",
        "channel": "twilio",
        "our_identity": "+15550001111",
        "origin": "send",
        "sent_by": "notify",
        "inbound_text": "",
        "answer_status": "answered",
        "answer": "hello",
        "delivery_status": DeliveryStatus.SENDING,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return ConversationRecord(**fields)


def _client(**overrides) -> ConversationRecord:
    return _send(origin="client", sent_by=None, inbound_text="hi", **overrides)


def _ask(**overrides) -> AskSend:
    fields: dict = {
        "interaction_id": "i-1",
        "answer_format": "text",
        "sensitive": False,
        "attempts": 1,
        "timed_out_attempts": [],
    }
    fields.update(overrides)
    return AskSend(**fields)


def test_send_record_is_valid_in_sending():
    record = _send()
    assert record.origin == "send"
    assert record.sent_by == "notify"
    assert record.delivery_status is DeliveryStatus.SENDING


def test_send_states_are_ordered_as_documented():
    assert DeliveryStatus.SENDING not in TERMINAL_STATUSES
    assert DeliveryStatus.OUTCOME_UNKNOWN in TERMINAL_STATUSES
    # A send record carries its answer in both states.
    assert DeliveryStatus.SENDING not in ANSWERLESS_STATUSES
    assert DeliveryStatus.OUTCOME_UNKNOWN not in ANSWERLESS_STATUSES


def test_send_record_requires_sent_by():
    with pytest.raises(ValueError, match="sent_by"):
        _send(sent_by=None)


def test_sent_by_is_refused_on_other_origins():
    with pytest.raises(ValueError, match="sent_by"):
        _send(origin="client", inbound_text="hi", delivery_status=DeliveryStatus.PENDING_DELIVERY)


def test_send_record_refuses_an_inbound():
    with pytest.raises(ValueError, match="send record carries no inbound_text"):
        _send(inbound_text="hi")
    with pytest.raises(ValueError, match="send record carries no inbound"):
        _send(inbound_form={"a": 1})


def test_send_record_is_always_answered():
    with pytest.raises(ValueError, match="send record is always answered"):
        _send(answer_status="error")


@pytest.mark.parametrize("status", [DeliveryStatus.SENDING, DeliveryStatus.OUTCOME_UNKNOWN])
def test_send_only_states_are_refused_on_other_origins(status):
    with pytest.raises(ValueError, match="only a send record"):
        _client(delivery_status=status)
    with pytest.raises(ValueError, match="only a send record"):
        _send(
            origin="operator",
            sent_by=None,
            caller_principal="op-1",
            delivery_status=status,
        )


def test_turn_message_id_is_only_on_sends():
    assert _send(turn_message_id="m-turn").turn_message_id == "m-turn"
    with pytest.raises(ValueError, match="turn_message_id"):
        _client(delivery_status=DeliveryStatus.PENDING_DELIVERY, turn_message_id="m-turn")


def test_ask_fields_ride_only_an_ask_send():
    record = _send(sent_by="ask", ask=_ask())
    assert record.ask is not None
    with pytest.raises(ValueError, match="ask"):
        _send(sent_by="ask")
    with pytest.raises(ValueError, match="ask"):
        _send(sent_by="notify", ask=_ask())


def test_ask_reply_needs_an_ask():
    reply = AskReply(answer="yes", medium_message_id="wamid.1", answered_at=_REPLY_AT)
    with pytest.raises(ValueError, match="ask_reply"):
        _send(sent_by="notify", ask_reply=reply)


@pytest.mark.parametrize("status", [DeliveryStatus.FAILED, DeliveryStatus.OUTCOME_UNKNOWN])
def test_ask_reply_is_refused_with_a_failed_end(status):
    reply = AskReply(answer="yes", medium_message_id="wamid.1", answered_at=_REPLY_AT)
    with pytest.raises(ValueError, match="ask_reply"):
        _send(sent_by="ask", ask=_ask(), ask_reply=reply, delivery_status=status)


@pytest.mark.parametrize("status", [DeliveryStatus.SENDING, DeliveryStatus.PROVISIONAL, DeliveryStatus.DELIVERED])
def test_ask_reply_rides_a_sent_or_delivered_ask(status):
    reply = AskReply(answer="yes", medium_message_id="wamid.1", answered_at=_REPLY_AT)
    record = _send(sent_by="ask", ask=_ask(), ask_reply=reply, delivery_status=status)
    assert record.ask_reply == reply


def test_sensitive_ask_refuses_a_reply_body():
    with pytest.raises(ValueError, match="sensitive"):
        _send(
            sent_by="ask",
            ask=_ask(sensitive=True),
            ask_reply=AskReply(answer="s3cret", medium_message_id="wamid.1", answered_at=_REPLY_AT),
            delivery_status=DeliveryStatus.DELIVERED,
        )
    record = _send(
        sent_by="ask",
        ask=_ask(sensitive=True),
        ask_reply=AskReply(answer=None, medium_message_id="wamid.1", answered_at=_REPLY_AT),
        delivery_status=DeliveryStatus.DELIVERED,
    )
    assert record.ask_reply is not None
    assert record.ask_reply.answer is None


def test_sends_pair_each_part_with_its_ids():
    record = _send(
        delivery_status=DeliveryStatus.PROVISIONAL,
        sends=[SentMessage(part=0, chars=5, provider_message_ids=["SM1"])],
    )
    assert record.sends[0].provider_message_ids == ["SM1"]


def test_caller_view_carries_the_send_fields_and_withholds_sends():
    record = _send(
        sent_by="ask",
        caller_principal="key-1",
        turn_message_id="m-turn",
        ask=_ask(sensitive=False),
        ask_reply=AskReply(answer="yes", medium_message_id="wamid.1", answered_at=_REPLY_AT),
        delivery_status=DeliveryStatus.PROVISIONAL,
        sends=[SentMessage(part=0, chars=5, provider_message_ids=["SM1"])],
        inbound_message_id=None,
    )
    view = record.caller_view()
    assert view["sent_by"] == "ask"
    assert view["turn_message_id"] == "m-turn"
    assert view["ask"] == {"sensitive": False}
    assert view["ask_reply"] == {"answer": "yes", "reply_to": None}
    assert view["caller_principal"] == "key-1"
    assert "sends" not in view
    admin = record.view()
    assert admin["sends"] == [{"part": 0, "chars": 5, "provider_message_ids": ["SM1"]}]
    assert admin["ask"] == {
        "interaction_id": "i-1",
        "answer_format": "text",
        "sensitive": False,
        "attempts": 1,
        "timed_out_attempts": [],
    }


def test_caller_view_of_a_plain_send_shows_no_ask():
    view = _send().caller_view()
    assert view["ask"] is None
    assert view["ask_reply"] is None
