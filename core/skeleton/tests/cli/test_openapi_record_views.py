"""The emitted conversation-record schema accepts what each read door serves.

An admin read serves :meth:`ConversationRecord.view`, a caller-scoped read
:meth:`ConversationRecord.caller_view`; both go out under ``ConversationRecordView`` (one
record) and ``TranscriptEnvelope`` (a page), so every record shape is validated against the
spec's own component schemas.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tai42_skeleton.conversations.models import AskReply, AskSend, ConversationRecord, DeliveryStatus, SentMessage


def _validator(spec: dict, component: str) -> Draft202012Validator:
    schema = {"$ref": f"#/components/schemas/{component}", "components": spec["components"]}
    return Draft202012Validator(schema)


def _client() -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id="m1",
        route_name="chat",
        door="channel",
        thread_id="bridge:chat:+1",
        client_address="+1",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="hi",
        answer_status="answered",
        answer="hello",
        delivery_status=DeliveryStatus.DELIVERED,
        error="internal detail",
        created_at=now,
        updated_at=now,
    )


def _ask_send(*, sensitive: bool) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id="s1",
        route_name="chat",
        door="channel",
        thread_id="bridge:chat:+1",
        client_address="+1",
        channel="twilio",
        our_identity="+15550001111",
        origin="send",
        sent_by="ask",
        inbound_text="",
        answer_status="answered",
        answer="proceed?",
        delivery_status=DeliveryStatus.DELIVERED,
        sends=[SentMessage(part=0, chars=8, provider_message_ids=["sid-1"])],
        ask=AskSend(
            interaction_id="i-1", answer_format="text", sensitive=sensitive, attempts=2, timed_out_attempts=[1]
        ),
        ask_reply=AskReply(
            answer=None if sensitive else "yes",
            medium_message_id="reply-1",
            answered_at=datetime(2027, 1, 15, 8, 0, tzinfo=UTC),
        ),
        created_at=now,
        updated_at=now,
    )


_RECORDS = {
    "client": _client,
    "ask": lambda: _ask_send(sensitive=False),
    "sensitive-ask": lambda: _ask_send(sensitive=True),
}


def _errors(validator: Draft202012Validator, payload: dict) -> list[str]:
    return [f"{'.'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(payload)]


@pytest.mark.parametrize("audience", ["admin", "caller"])
@pytest.mark.parametrize("record", sorted(_RECORDS))
def test_a_served_record_matches_the_declared_record_view(spec: dict, record: str, audience: str) -> None:
    built = _RECORDS[record]()
    served = built.view() if audience == "admin" else built.caller_view()
    assert _errors(_validator(spec, "ConversationRecordView"), served) == []


@pytest.mark.parametrize("audience", ["admin", "caller"])
def test_a_served_transcript_page_matches_the_declared_envelope(spec: dict, audience: str) -> None:
    built = [make() for make in _RECORDS.values()]
    page = {
        "items": [r.view() if audience == "admin" else r.caller_view() for r in built],
        "total": len(built),
        "page": 1,
        "page_size": 50,
        "next_page": None,
        "order": "asc",
        "truncated": False,
        "unreadable": 0,
        "person_id": None,
    }
    assert _errors(_validator(spec, "TranscriptEnvelope"), page) == []


def _leaf_paths(errors) -> set[str]:
    paths: set[str] = set()
    for error in errors:
        if error.context:
            paths |= _leaf_paths(error.context)
        else:
            paths.add(".".join(map(str, error.absolute_path)))
    return paths


def test_the_admin_record_view_types_every_ask_field(spec: dict) -> None:
    served: dict[str, Any] = _ask_send(sensitive=False).view()
    served["ask"]["attempts"] = "two"
    served["ask_reply"]["answered_at"] = 5
    validator = _validator(spec, "ConversationRecordView")
    # Each wrong field is refused inside its object (the other leaf is the object's ``null`` branch).
    assert _leaf_paths(validator.iter_errors(served)) == {"ask", "ask.attempts", "ask_reply", "ask_reply.answered_at"}
