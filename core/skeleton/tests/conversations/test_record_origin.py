"""The ``origin`` field on a conversation record: a ``client`` record answers a non-blank
inbound; an ``operator`` record carries no inbound, is always answered, names its sender, and
publishes ``origin`` in the caller-safe view.
"""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus


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
        "inbound_text": "ask",
        "answer_status": "answered",
        "answer": "hi",
        "delivery_status": DeliveryStatus.PENDING_DELIVERY,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return ConversationRecord(**fields)


def test_origin_is_required():
    # Clean-break: origin carries no default, so a construction omitting it fails loudly and a
    # stored blob missing it is rejected the same way.
    fields = {k: v for k, v in _record().model_dump().items() if k != "origin"}
    with pytest.raises(ValueError, match="origin"):
        ConversationRecord(**fields)


def test_client_record_refuses_a_blank_inbound_text():
    with pytest.raises(ValueError, match="non-blank inbound_text"):
        _record(inbound_text="   ")


def test_operator_record_is_valid_with_empty_inbound_and_answered():
    record = _record(origin="operator", inbound_text="", caller_principal="op-1")
    assert record.origin == "operator"
    assert record.inbound_text == ""


def test_operator_record_refuses_a_nonempty_inbound_text():
    with pytest.raises(ValueError, match="operator record carries no inbound_text"):
        _record(origin="operator", inbound_text="something", caller_principal="op-1")


def test_operator_record_must_be_answered():
    with pytest.raises(ValueError, match="operator record is always answered"):
        _record(
            origin="operator",
            inbound_text="",
            caller_principal="op-1",
            answer_status="silent",
            answer=None,
        )


def test_operator_record_requires_the_sending_principal():
    with pytest.raises(ValueError, match="must name the operator"):
        _record(origin="operator", inbound_text="", caller_principal=None)


def test_operator_record_still_requires_non_blank_answer():
    with pytest.raises(ValueError, match="non-blank answer text"):
        _record(origin="operator", inbound_text="", caller_principal="op-1", answer="  ")


def test_caller_view_publishes_origin():
    view = _record(origin="operator", inbound_text="", caller_principal="op-1").caller_view()
    assert view["origin"] == "operator"
    assert view["inbound_text"] == ""


def test_client_address_accepts_a_long_legitimate_value():
    # 256 chars is comfortably above any real address (phone / visitor id / email).
    record = _record(client_address="a" * 256)
    assert len(record.client_address) == 256


def test_client_address_refuses_an_oversized_value():
    # ``client_address`` is embedded verbatim into Redis key names, so an oversized
    # value is refused loudly rather than forming an unbounded key.
    with pytest.raises(ValueError, match="client_address"):
        _record(client_address="a" * 257)
