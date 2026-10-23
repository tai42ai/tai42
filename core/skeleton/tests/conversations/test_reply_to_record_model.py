"""The quoted message on the conversation record model: ``inbound_reply_to`` on an inbound record,
``reply_to`` on an ask reply, and both in the admin and caller-scoped views.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from tai42_contract.conversations import ReplyTo, ReplyToStatus

from tai42_skeleton.conversations.models import AskReply, AskSend, ConversationRecord, DeliveryStatus

_QUOTE = ReplyTo(message_id="wamid.0", status=ReplyToStatus.FOUND, text="the earlier message")
_QUOTE_JSON = {"message_id": "wamid.0", "status": "found", "text": "the earlier message"}


def _record(**overrides) -> ConversationRecord:
    now = time.time()
    fields: dict = {
        "message_id": "m1",
        "route_name": "chat",
        "door": "channel",
        "thread_id": "bridge:chat:+1",
        "client_address": "+1",
        "channel": "twilio",
        "our_identity": "+15550001111",
        "origin": "client",
        "inbound_text": "a reply",
        "answer_status": "answered",
        "answer": "hi",
        "delivery_status": DeliveryStatus.PENDING_DELIVERY,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return ConversationRecord(**fields)


def test_a_client_record_carries_its_quoted_message():
    record = _record(inbound_reply_to=_QUOTE)
    assert record.inbound_reply_to == _QUOTE
    assert _record().inbound_reply_to is None
    assert ConversationRecord.model_validate_json(record.model_dump_json()) == record


def test_an_operator_record_refuses_a_quoted_message():
    with pytest.raises(ValueError, match="operator record carries no inbound_reply_to"):
        _record(origin="operator", inbound_text="", caller_principal="op-1", inbound_reply_to=_QUOTE)


def test_a_send_record_refuses_a_quoted_message():
    with pytest.raises(ValueError, match="send record carries no inbound"):
        _record(
            origin="send",
            sent_by="notify",
            inbound_text="",
            delivery_status=DeliveryStatus.SENDING,
            inbound_reply_to=_QUOTE,
        )


def test_an_event_record_refuses_a_quoted_message():
    with pytest.raises(ValueError, match="event record carries no inbound_reply_to"):
        _record(
            inbound_kind="event",
            inbound_text="",
            inbound_event={"id": "e1", "kind": "status", "payload": {}},
            inbound_reply_to=_QUOTE,
        )


def test_both_views_carry_the_quoted_message():
    record = _record(inbound_reply_to=_QUOTE)
    assert record.view()["inbound_reply_to"] == _QUOTE_JSON
    assert record.caller_view()["inbound_reply_to"] == _QUOTE_JSON
    assert _record().caller_view()["inbound_reply_to"] is None


def _ask_send(*, sensitive: bool, reply: AskReply) -> ConversationRecord:
    return _record(
        origin="send",
        sent_by="ask",
        inbound_text="",
        delivery_status=DeliveryStatus.DELIVERED,
        ask=AskSend(interaction_id="i-1", answer_format="text", sensitive=sensitive),
        ask_reply=reply,
    )


def test_an_ask_reply_carries_its_quoted_message_in_both_views():
    reply = AskReply(answer="yes", medium_message_id="wamid.1", answered_at=datetime(2026, 1, 1, tzinfo=UTC))
    record = _ask_send(sensitive=False, reply=reply.model_copy(update={"reply_to": _QUOTE}))
    admin_reply = record.view()["ask_reply"]
    assert isinstance(admin_reply, dict)
    assert admin_reply["reply_to"] == _QUOTE_JSON
    assert record.caller_view()["ask_reply"] == {"answer": "yes", "reply_to": _QUOTE_JSON}
    assert reply.reply_to is None


def test_a_sensitive_ask_reply_keeps_its_quoted_message_without_its_body():
    reply = AskReply(answer=None, reply_to=_QUOTE, answered_at=datetime(2026, 1, 1, tzinfo=UTC))
    record = _ask_send(sensitive=True, reply=reply)
    assert record.caller_view()["ask_reply"] == {"answer": None, "reply_to": _QUOTE_JSON}
