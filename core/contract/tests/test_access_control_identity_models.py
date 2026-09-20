"""Tests for the ``Principal`` and ``IdentityRecord`` contract shapes.

``Principal`` is the unit of identity every credential belongs to; ``IdentityRecord``
is the stored api-key record, which now declares a REQUIRED ``owner_user_id`` because
every api key belongs to a principal. These pin the validated shape, its defaults, and
its fail-loud validation — not any store or enforcement (skeleton).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tai42_contract.access_control import IdentityRecord, Principal


def test_principal_exported_from_package():
    from tai42_contract.access_control import Principal as Exported

    assert Exported is Principal


def test_principal_minimal_and_defaults():
    principal = Principal(
        user_id="usr-1",
        kind="human",
        display_name="Alice",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert principal.created_by is None
    assert principal.disabled is False


def test_principal_kind_is_a_closed_enum():
    with pytest.raises(ValidationError):
        Principal(
            user_id="usr-1",
            kind="robot",  # pyright: ignore[reportArgumentType]
            display_name="Alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_principal_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        Principal(
            user_id="usr-1",
            kind="service",
            display_name="Batch runner",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            nonsense=True,  # pyright: ignore[reportCallIssue]
        )


def test_principal_created_at_is_required():
    with pytest.raises(ValidationError):
        Principal(user_id="usr-1", kind="human", display_name="Alice")  # pyright: ignore[reportCallIssue]


def test_principal_rejects_naive_created_at():
    with pytest.raises(ValidationError):
        Principal(
            user_id="usr-1",
            kind="human",
            display_name="Alice",
            created_at=datetime(2026, 1, 1),  # naive
        )


def test_principal_normalizes_created_at_to_utc():
    plus_two = timezone(timedelta(hours=2))
    principal = Principal(
        user_id="usr-1",
        kind="human",
        display_name="Alice",
        created_at=datetime(2026, 1, 1, 12, tzinfo=plus_two),
    )
    assert principal.created_at.tzinfo is UTC
    assert principal.created_at == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_identity_record_requires_owner_user_id():
    with pytest.raises(ValidationError):
        IdentityRecord(user_id="usr-1-key")  # pyright: ignore[reportCallIssue]


def test_identity_record_carries_owner_and_extra_claims():
    # Records are built from the provider's stored dict; extra keys ride as claims.
    record = IdentityRecord.model_validate(
        {"user_id": "usr-1-key", "owner_user_id": "usr-1", "email": "a@x.test", "description": "laptop"}
    )
    assert record.owner_user_id == "usr-1"
    # Extra fields ride as identity claims (extra="allow").
    assert record.model_dump()["email"] == "a@x.test"
    assert record.model_dump()["description"] == "laptop"
