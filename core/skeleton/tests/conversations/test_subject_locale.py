"""The conversation turn carries the subject's locale onto the payload's subject block and
the ambient state context, resolving person-stored over channel-supplied over the route's
operator-declared default over none — so the rendering layer resolves a language the flow
never selects."""

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


def _route(locale: str | None = None) -> ConversationRoute:
    return ConversationRoute(
        route_name="r",
        door="channel",
        target_kind="tool",
        target_name="a",
        execution_key="svc",
        channel="telegram",
        our_identity="42",
        execution_key_fingerprint="fp",
        locale=locale,
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


def test_resolved_locale_prefers_person_over_channel_over_route_over_none() -> None:
    # A person's stored locale wins over every other source, route default included.
    assert _resolved_locale(_person("en"), _record("he"), _route("fr")) == "en"
    # A per-turn channel hint wins over the route default (person absent).
    assert _resolved_locale(_person(None), _record("he"), _route("fr")) == "he"
    assert _resolved_locale(None, _record("he"), _route("fr")) == "he"
    # No person, no per-turn hint: the route's operator-declared default fills.
    assert _resolved_locale(_person(None), _record(None), _route("fr")) == "fr"
    assert _resolved_locale(None, _record(None), _route("fr")) == "fr"
    # No source at all: the explicit "no locale known" the renderer never defaults away.
    assert _resolved_locale(_person(None), _record(None), _route()) is None


def test_route_default_fills_a_channel_turn_with_no_per_turn_locale() -> None:
    # A channel turn that supplies no per-turn locale (WhatsApp/Twilio/Slack) and no stored
    # contact override renders templated parts in the route's declared language, not English.
    block = _turn_block(_record(None), _route("fr"), person=_person(None), thread_id="th")
    subject = block["subject"]
    assert isinstance(subject, dict)
    assert subject["locale"] == "fr"


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


def test_state_context_candidates_fall_back_to_route_default() -> None:
    ctx = _conversation_state_context(_route("fr"), _record(None), _person(None), actor="u")
    assert ctx.candidates.locale == "fr"
