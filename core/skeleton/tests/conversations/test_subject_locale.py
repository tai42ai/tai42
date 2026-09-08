"""The conversation turn carries the subject's locale onto the payload's subject block and
the ambient state context, resolving person-stored over channel-supplied over none — so the
rendering layer resolves a language the flow never selects."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from tai42_contract.conversations import ConversationRoute, Person, PersonAddress

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn import (
    _conversation_state_context,
    _resolved_locale,
    _turn_block,
)


def _route() -> ConversationRoute:
    return ConversationRoute(
        route_name="r",
        door="channel",
        target_kind="tool",
        target_name="a",
        execution_key="svc",
        channel="telegram",
        our_identity="42",
        execution_key_fingerprint="fp",
    )


def _record(locale: str | None) -> ConversationRecord:
    return ConversationRecord(
        message_id="m",
        route_name="r",
        door="channel",
        thread_id="th",
        client_address="c",
        channel="telegram",
        our_identity="42",
        provider_message_id="1",
        origin="client",
        inbound_text="hi",
        inbound_locale=locale,
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=time.time(),
        updated_at=time.time(),
    )


def _person(locale: str | None) -> Person:
    return Person(
        person_id="p1",
        target_kind="tool",
        target_name="a",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["r"],
                channel="telegram",
                our_identity="42",
                address="c",
                linked_at=datetime.now(UTC),
            )
        ],
        locale=locale,
    )


def test_resolved_locale_prefers_person_over_channel_over_none() -> None:
    assert _resolved_locale(_person("en"), _record("he")) == "en"  # operator override wins
    assert _resolved_locale(_person(None), _record("he")) == "he"  # channel fills
    assert _resolved_locale(None, _record("he")) == "he"  # no person -> channel
    assert _resolved_locale(_person(None), _record(None)) is None  # explicit none


def test_turn_block_subject_carries_locale() -> None:
    block = _turn_block(_record("he"), _route(), person=_person(None), thread_id="th")
    subject = block["subject"]
    assert isinstance(subject, dict)
    assert subject["locale"] == "he"


def test_state_context_candidates_carry_locale() -> None:
    ctx = _conversation_state_context(_route(), _record("he"), _person(None), actor="u")
    assert ctx.candidates.locale == "he"
    ctx_none = _conversation_state_context(_route(), _record(None), None, actor="u")
    assert ctx_none.candidates.locale is None
