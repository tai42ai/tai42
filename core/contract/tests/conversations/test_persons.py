"""Tests for the multichannel identity models: ``PersonAddress`` and ``Person``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError


def _address_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "door": "channel",
        "routes": ["line"],
        "channel": "twilio",
        "our_identity": "+15550001111",
        "address": "+15550002222",
        "linked_at": datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_person_address_channel_row_carries_channel_and_identity():
    from tai42_contract.conversations import PersonAddress

    addr = PersonAddress(**_address_kwargs())
    assert addr.channel == "twilio"
    assert addr.our_identity == "+15550001111"
    with pytest.raises(ValidationError):
        addr.address = "changed"  # frozen


def test_person_address_api_row_carries_no_channel_identity():
    from tai42_contract.conversations import PersonAddress

    addr = PersonAddress(**_address_kwargs(door="api", channel=None, our_identity=None, address="svc/end"))
    assert addr.channel is None
    assert addr.our_identity is None
    # An api row that smuggles a channel/identity is refused.
    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(door="api", channel="twilio", our_identity=None, address="svc/end"))


def test_person_address_channel_row_requires_channel_and_identity():
    from tai42_contract.conversations import PersonAddress

    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(channel=None))
    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(our_identity="   "))


@pytest.mark.parametrize("routes", [[], ["", "line"], ["dup", "dup"]])
def test_person_address_routes_must_be_non_empty_non_blank_and_unique(routes: list[str]):
    from tai42_contract.conversations import PersonAddress

    with pytest.raises(ValidationError):
        PersonAddress(**_address_kwargs(routes=routes))


def test_person_address_serializes_linked_at_as_isoformat():
    from tai42_contract.conversations import PersonAddress

    addr = PersonAddress(**_address_kwargs())
    assert '"linked_at":"2026-08-08T12:00:00+00:00"' in addr.model_dump_json()


def test_person_requires_at_least_one_address_and_serializes_sortable_created_at():
    from tai42_contract.conversations import Person, PersonAddress

    with pytest.raises(ValidationError):
        Person(
            person_id="p1",
            target_kind="agent",
            target_name="assistant",
            created_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
            addresses=[],
        )
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="assistant",
        created_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        addresses=[PersonAddress(**_address_kwargs())],
    )
    # ``+00:00`` (isoformat), never the default ``Z`` — the merge survivor rule compares this
    # string lexically.
    assert '"created_at":"2026-08-08T12:00:00+00:00"' in person.model_dump_json()


def test_person_created_at_rejects_naive_and_normalizes_non_utc_to_offset():
    from tai42_contract.conversations import Person, PersonAddress

    # A naive created_at has no offset and would sort before every ``+00:00`` — reject it,
    # so the merge survivor rule never compares an offset-less string.
    with pytest.raises(ValidationError, match="timezone-aware"):
        Person(
            person_id="p1",
            target_kind="agent",
            target_name="assistant",
            created_at=datetime(2026, 8, 8, 12, 0),  # naive
            addresses=[PersonAddress(**_address_kwargs())],
        )
    # A non-UTC aware value is normalized to UTC, so the serialized form still carries the
    # canonical ``+00:00`` offset and stays lexically sortable against every other row.
    eastern = timezone(timedelta(hours=-5))
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="assistant",
        created_at=datetime(2026, 8, 8, 7, 0, tzinfo=eastern),  # == 12:00 UTC
        addresses=[PersonAddress(**_address_kwargs())],
    )
    assert person.created_at.utcoffset() == timedelta(0)
    assert '"created_at":"2026-08-08T12:00:00+00:00"' in person.model_dump_json()


def test_person_address_linked_at_rejects_naive_and_normalizes_non_utc_to_offset():
    from tai42_contract.conversations import PersonAddress

    with pytest.raises(ValidationError, match="timezone-aware"):
        PersonAddress(**_address_kwargs(linked_at=datetime(2026, 8, 8, 12, 0)))  # naive
    eastern = timezone(timedelta(hours=-5))
    addr = PersonAddress(**_address_kwargs(linked_at=datetime(2026, 8, 8, 7, 0, tzinfo=eastern)))
    assert addr.linked_at.utcoffset() == timedelta(0)
    assert '"linked_at":"2026-08-08T12:00:00+00:00"' in addr.model_dump_json()
