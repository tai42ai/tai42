"""Tests for ``Correlation`` (the one-pending-per-address parked-ask record) and the
``CorrelationStore`` port."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


def _correlation_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "callback_url": "https://host.example/api/interactions/callback/tkt",
        "interaction_id": "int-1",
        "ttl_deadline": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_correlation_roundtrips_its_fields():
    from tai42_contract.channels import Correlation

    entry = Correlation(**_correlation_kwargs())
    assert entry.callback_url == "https://host.example/api/interactions/callback/tkt"
    assert entry.interaction_id == "int-1"
    assert entry.ttl_deadline == datetime(2026, 1, 1, tzinfo=UTC)


def test_correlation_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import Correlation

    entry = Correlation(**_correlation_kwargs())
    with pytest.raises(ValidationError):
        entry.interaction_id = "other"


def test_correlation_ttl_deadline_must_be_tz_aware():
    from pydantic import ValidationError

    from tai42_contract.channels import Correlation

    with pytest.raises(ValidationError, match="timezone-aware"):
        Correlation(**_correlation_kwargs(ttl_deadline=datetime(2026, 1, 1)))


def test_correlation_ttl_deadline_normalized_to_utc():
    from datetime import timedelta, timezone

    from tai42_contract.channels import Correlation

    plus_two = timezone(timedelta(hours=2))
    entry = Correlation(**_correlation_kwargs(ttl_deadline=datetime(2026, 1, 1, 3, tzinfo=plus_two)))
    # Normalized to UTC, matching ChannelDelivery.timeout_at.
    assert entry.ttl_deadline == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert entry.ttl_deadline.tzinfo is UTC


def test_correlation_store_is_runtime_checkable_and_shaped():
    from tai42_contract.channels import Correlation, CorrelationStore

    class _Ok:
        async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
            return True

        async def get_correlation(self, key: str) -> Correlation | None:
            return None

        async def release_correlation(self, key: str) -> None:
            return None

    class _MissingRelease:
        async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
            return True

        async def get_correlation(self, key: str) -> Correlation | None:
            return None

    # A fake implementing all three methods conforms structurally; one missing a
    # method does not — the port is the full set/get/release surface, never partial.
    assert isinstance(_Ok(), CorrelationStore)
    assert not isinstance(_MissingRelease(), CorrelationStore)
    assert not isinstance(object(), CorrelationStore)


def test_correlation_store_is_not_an_extension_of_channel():
    # The port stands alone: a Channel is NOT structurally a CorrelationStore and a
    # CorrelationStore is NOT structurally a Channel — a channel implements the store
    # separately (or not at all), never off the channel instance.
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification, Correlation, CorrelationStore

    class _Channel:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    class _Store:
        async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
            return True

        async def get_correlation(self, key: str) -> Correlation | None:
            return None

        async def release_correlation(self, key: str) -> None:
            return None

    assert isinstance(_Channel(), Channel)
    assert not isinstance(_Channel(), CorrelationStore)
    assert isinstance(_Store(), CorrelationStore)
    assert not isinstance(_Store(), Channel)


def test_correlation_types_exported():
    import tai42_contract.channels as channels_module

    assert "Correlation" in channels_module.__all__
    assert "CorrelationStore" in channels_module.__all__
