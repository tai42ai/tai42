"""Record-outcome helpers: run attribution and the greeting-prepend composition."""

from __future__ import annotations

from datetime import UTC, datetime

from tai42_contract.conversations import (
    Person,
    PersonAddress,
)

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn import context as context_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import pairing as pairing_module

from .conftest import (
    _api_route,
    _channel_route,
)


def test_with_greeting_prepends_a_leading_part():
    # A due first-contact greeting is its OWN leading message ahead of the turn's parts (the
    # joined answer stays byte-identical to the old "greeting\n\nanswer" prefix).
    outcome = outcome_module._ResolvedOutcome(
        answer_status="answered",
        parts=[outcome_module._text_part("first"), outcome_module._text_part("second")],
        error=None,
    )
    greeted = pairing_module._with_greeting(outcome, "Welcome!")
    assert isinstance(greeted, outcome_module._ResolvedOutcome)
    assert [p.message for p in greeted.parts] == ["Welcome!", "first", "second"]
    assert greeted.answer == "Welcome!\n\nfirst\n\nsecond"


def test_with_greeting_on_a_silent_outcome_is_greeting_only():
    greeted = pairing_module._with_greeting(outcome_module._SilentOutcome(), "Welcome!")
    assert isinstance(greeted, outcome_module._ResolvedOutcome)
    assert [p.message for p in greeted.parts] == ["Welcome!"]


def _attribution_intake(**overrides):

    base = {
        "message_id": "m1",
        "route_name": "line",
        "door": "channel",
        "thread_id": "bridge:line:+15550002222",
        "client_address": "+15550002222",
        "channel": "twilio",
        "our_identity": "+15550001111",
        "origin": "client",
        "inbound_text": "hi",
        # An admitted intake carries no outcome yet (an answerless status).
        "delivery_status": DeliveryStatus.ACCEPTED,
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    base.update(overrides)
    return ConversationRecord(**base)


def test_conversation_attribution_falls_back_to_channel_address_without_a_person():
    route = _channel_route()
    intake = _attribution_intake()
    attribution = context_module._conversation_attribution(route, intake, None)
    # No person -> raw {channel}:{client_address} user, resolved thread as session.
    assert attribution.user_id == "twilio:+15550002222"
    assert attribution.session_id == "bridge:line:+15550002222"
    assert attribution.tags == ["route:line"]
    assert attribution.metadata == {"channel": "twilio", "our_identity": "+15550001111"}


def test_conversation_attribution_prefers_person_id_and_person_thread():

    route = _channel_route()
    # A LINKED person aggregates to the @person thread; the intake carries that resolved thread.
    intake = _attribution_intake(thread_id="bridge:@person:person-42")
    now = datetime.now(UTC)
    person = Person(
        person_id="person-42",
        target_kind="agent",
        target_name="echo",
        created_at=now,
        addresses=[
            PersonAddress(
                door="channel",
                channel="twilio",
                our_identity="+15550001111",
                address="+15550002222",
                routes=["line"],
                linked_at=now,
            ),
        ],
    )
    attribution = context_module._conversation_attribution(route, intake, person)
    assert attribution.user_id == "person-42"  # person-id-first
    assert attribution.session_id == "bridge:@person:person-42"


def test_conversation_attribution_api_door_omits_none_metadata():
    route = _api_route()
    # An api-door intake has no channel/our_identity — those metadata keys are omitted,
    # never emitted as literal None (which the backend would drop with a warning).
    intake = _attribution_intake(
        door="api", route_name="chat", channel=None, our_identity=None, thread_id="bridge:chat:caller:addr"
    )
    attribution = context_module._conversation_attribution(route, intake, None)
    assert attribution.metadata == {}
    # No person and no channel -> the documented raw fallback (channel is None here).
    assert attribution.user_id == "+15550002222"
    assert attribution.session_id == "bridge:chat:caller:addr"
    assert attribution.tags == ["route:chat"]
