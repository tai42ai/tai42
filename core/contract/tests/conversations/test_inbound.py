"""Tests for the client-facing inbound bodies: ``ConversationMessage``,
``ConversationEvent``, ``ConversationEventSubmission``, and ``BlankInboundTextError``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

# -- ConversationMessage --------------------------------------------------------


def test_conversation_message_is_frozen_and_carries_the_inbound_body():
    from tai42_contract.conversations import ConversationMessage

    msg = ConversationMessage(external_user_id="u-1", text="hello there")
    assert msg.external_user_id == "u-1"
    with pytest.raises(ValidationError):
        msg.text = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_conversation_message_rejects_blank_fields(blank: str):
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError):
        ConversationMessage(external_user_id=blank, text="hi")
    with pytest.raises(ValidationError):
        ConversationMessage(external_user_id="u-1", text=blank)


def test_conversation_message_accepts_none_and_valid_params():
    from tai42_contract.conversations import ConversationMessage

    assert ConversationMessage(external_user_id="u-1", text="hi").params is None
    msg = ConversationMessage(external_user_id="u-1", text="hi", params={"token": "abc"})
    assert msg.params == {"token": "abc"}


def test_conversation_message_refuses_invalid_params():
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError):
        ConversationMessage(external_user_id="u-1", text="hi", params={"bad key": "v"})


def test_conversation_message_accepts_none_and_a_valid_form():
    from tai42_contract.conversations import ConversationMessage

    assert ConversationMessage(external_user_id="u1", text="hi").form is None
    message = ConversationMessage(external_user_id="u1", text="size: L", form={"size": "L"})
    assert message.form == {"size": "L"}


def test_conversation_message_form_still_requires_non_blank_text():
    # ``text`` is the carrier every reader consumes; a structured submission never
    # replaces it — a channel submits a faithful text form alongside the data.
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError, match="must be non-blank"):
        ConversationMessage(external_user_id="u1", text="   ", form={"size": "L"})


def test_conversation_message_refuses_an_invalid_form():
    from tai42_contract.conversations import ConversationMessage

    with pytest.raises(ValidationError, match="must be finite"):
        ConversationMessage(external_user_id="u1", text="hi", form={"x": float("nan")})


# -- ConversationEvent / ConversationEventSubmission ----------------------------


def test_conversation_event_round_trips_and_defaults_payload():
    from tai42_contract.conversations import ConversationEvent

    event = ConversationEvent(event_id="evt-1", kind="provider.update")
    assert event.event_id == "evt-1"
    assert event.kind == "provider.update"
    assert event.payload == {}
    with pytest.raises(ValidationError):
        event.event_id = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_conversation_event_refuses_blank_event_id(blank: str):
    from tai42_contract.conversations import ConversationEvent

    with pytest.raises(ValidationError, match="event_id must be non-blank"):
        ConversationEvent(event_id=blank, kind="provider.update")


def test_conversation_event_refuses_overlong_event_id():
    from tai42_contract.conversations import EVENT_ID_MAX_CHARS, ConversationEvent

    with pytest.raises(ValidationError, match="at most"):
        ConversationEvent(event_id="e" * (EVENT_ID_MAX_CHARS + 1), kind="provider.update")


@pytest.mark.parametrize("bad_kind", ["", "has space", "bad/slash", "x" * 129])
def test_conversation_event_refuses_bad_kind(bad_kind: str):
    from tai42_contract.conversations import ConversationEvent

    with pytest.raises(ValidationError, match="kind must match"):
        ConversationEvent(event_id="evt-1", kind=bad_kind)


def test_conversation_event_refuses_unknown_keys():
    from tai42_contract.conversations import ConversationEvent

    with pytest.raises(ValidationError):
        ConversationEvent(event_id="evt-1", kind="k", extra="nope")  # type: ignore[call-arg]


def test_conversation_event_payload_bounds_reject_too_deep():
    from tai42_contract.conversations import ConversationEvent

    deep: dict[str, Any] = {}
    node = deep
    for _ in range(5000):
        child: dict[str, Any] = {}
        node["a"] = child
        node = child
    with pytest.raises(ValidationError, match="event payload nests deeper"):
        ConversationEvent(event_id="evt-1", kind="k", payload=deep)


def test_conversation_event_payload_bounds_reject_too_large():
    from tai42_contract.conversations import INBOUND_FORM_MAX_BYTES, ConversationEvent

    with pytest.raises(ValidationError, match="event payload serializes to"):
        ConversationEvent(event_id="evt-1", kind="k", payload={"x": "a" * (INBOUND_FORM_MAX_BYTES + 1)})


def test_conversation_event_submission_round_trips_by_address():
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    submission = ConversationEventSubmission(
        address="user-1", event=ConversationEvent(event_id="evt-1", kind="provider.update")
    )
    assert submission.address == "user-1"
    assert submission.thread_id is None
    assert submission.wait_seconds == 0
    assert submission.event.event_id == "evt-1"
    with pytest.raises(ValidationError):
        submission.address = "changed"


def test_conversation_event_submission_round_trips_by_thread_id():
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    submission = ConversationEventSubmission(
        thread_id="bridge:r:user-1", event=ConversationEvent(event_id="evt-1", kind="k"), wait_seconds=5
    )
    assert submission.thread_id == "bridge:r:user-1"
    assert submission.address is None
    assert submission.wait_seconds == 5


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither
        {"address": "user-1", "thread_id": "bridge:r:user-1"},  # both
        {"address": "   "},  # a blank address is not a thread ref
        {"address": "user-1", "thread_id": "   "},  # a blank-but-present thread_id is malformed
    ],
)
def test_conversation_event_submission_requires_exactly_one_thread_ref(kwargs: dict[str, Any]):
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    # A blank-but-present reference is malformed (the door branches on presence), so every
    # listed case refuses — the blank ones with the non-blank message, the rest with the
    # exactly-one message.
    event = ConversationEvent(event_id="evt-1", kind="k")
    blank = any(v is not None and not v.strip() for v in kwargs.values())
    expected = "must be non-blank when given" if blank else "exactly one of address or thread_id"
    with pytest.raises(ValidationError, match=expected):
        ConversationEventSubmission(event=event, **kwargs)


def test_conversation_event_submission_refuses_negative_wait_seconds():
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    with pytest.raises(ValidationError):
        ConversationEventSubmission(
            address="user-1", event=ConversationEvent(event_id="evt-1", kind="k"), wait_seconds=-1
        )


def test_conversation_event_submission_has_no_callback_field():
    from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission

    assert "callback_url" not in ConversationEventSubmission.model_fields
    with pytest.raises(ValidationError):
        ConversationEventSubmission(
            address="user-1",
            event=ConversationEvent(event_id="evt-1", kind="k"),
            callback_url="https://evil.example",  # type: ignore[call-arg]
        )


def test_blank_inbound_text_error_is_a_value_error():
    from tai42_contract.conversations import BlankInboundTextError

    assert issubclass(BlankInboundTextError, ValueError)
