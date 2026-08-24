"""The Twilio :class:`CorrelationStore` over the plugin-owned pending/seen keys.

Conformance for the contract port — a NX reserve, a non-destructive peek, and an
idempotent release round-trip on the fake redis, the one-pending-per-pair guarantee
the NX enforces — plus the ``reserve_pending``/``release_pending`` deliver-path front
door and the ``MessageSid`` dedupe set.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError, Correlation

from tai42_channel_twilio.correlation import (
    PendingQuestionExistsError,
    already_seen,
    correlation_key,
    mark_seen,
    release_pending,
    reserve_pending,
    twilio_correlation_store,
)

from .conftest import FakeRedis

pytestmark = pytest.mark.usefixtures("twilio_env")

_TWILIO = "+15550000001"
_HUMAN = "+15550000002"
_KEY = correlation_key(_TWILIO, _HUMAN)
_REDIS_KEY = f"channel:twilio:pending:{_KEY}"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _entry(callback_url: str = _CALLBACK, interaction_id: str = "int-1") -> Correlation:
    return Correlation(callback_url=callback_url, interaction_id=interaction_id, ttl_deadline=_deadline())


# -- the contract port: set (NX) / get (peek) / release (idempotent) -------------


async def test_set_get_round_trip(fake_redis: FakeRedis):
    entry = _entry(interaction_id="int-9")
    assert await twilio_correlation_store.set_correlation(_KEY, entry, ttl_seconds=300) is True

    got = await twilio_correlation_store.get_correlation(_KEY)
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-9"
    # The peek is non-destructive: the reservation survives for a second read.
    assert await twilio_correlation_store.get_correlation(_KEY) is not None


async def test_get_unknown_returns_none(fake_redis: FakeRedis):
    assert await twilio_correlation_store.get_correlation(_KEY) is None


async def test_set_is_nx_one_pending_per_pair(fake_redis: FakeRedis):
    first = await twilio_correlation_store.set_correlation(_KEY, _entry(interaction_id="first"), ttl_seconds=300)
    assert first is True
    # A second reserve on the held pair is refused — never a blind overwrite.
    second = await twilio_correlation_store.set_correlation(_KEY, _entry(interaction_id="second"), ttl_seconds=300)
    assert second is False
    held = await twilio_correlation_store.get_correlation(_KEY)
    assert held is not None
    assert held.interaction_id == "first"


async def test_release_frees_the_pair_and_is_idempotent(fake_redis: FakeRedis):
    await twilio_correlation_store.set_correlation(_KEY, _entry(), ttl_seconds=300)
    await twilio_correlation_store.release_correlation(_KEY)
    assert not fake_redis.store
    # A second release on the now-free pair is a no-op, never an error, and the pair
    # can be reserved again.
    await twilio_correlation_store.release_correlation(_KEY)
    assert await twilio_correlation_store.set_correlation(_KEY, _entry(), ttl_seconds=300) is True


# -- the deliver-path front door: reserve_pending / release_pending --------------


async def test_reserve_stores_the_correlation_record(fake_redis: FakeRedis):
    timeout_at = _deadline()
    await reserve_pending(_TWILIO, _HUMAN, _CALLBACK, "int-42", timeout_at)

    got = await twilio_correlation_store.get_correlation(_KEY)
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-42"  # the interaction id is stored (additive)


async def test_reservation_ttl_is_remaining_budget(fake_redis: FakeRedis):
    before = datetime.now(UTC)
    timeout_at = _deadline(120)
    await reserve_pending(_TWILIO, _HUMAN, _CALLBACK, "int-1", timeout_at)
    after = datetime.now(UTC)

    ttl = fake_redis.ttls[_REDIS_KEY]
    assert math.ceil((timeout_at - after).total_seconds()) <= ttl <= math.ceil((timeout_at - before).total_seconds())


async def test_double_reserve_rejected(fake_redis: FakeRedis):
    await reserve_pending(_TWILIO, _HUMAN, _CALLBACK, "int-1", _deadline())

    with pytest.raises(PendingQuestionExistsError, match="already pending"):
        await reserve_pending(_TWILIO, _HUMAN, "https://app.example/other", "int-2", _deadline())


async def test_reserve_past_deadline_raises_and_stores_nothing(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="already passed"):
        await reserve_pending(_TWILIO, _HUMAN, _CALLBACK, "int-1", _deadline(-1))

    assert not fake_redis.store  # nothing reserved for an expired budget


async def test_release_pending_frees_the_pair(fake_redis: FakeRedis):
    await reserve_pending(_TWILIO, _HUMAN, _CALLBACK, "int-1", _deadline())
    await release_pending(_TWILIO, _HUMAN)

    # Freed — a fresh reservation for the pair succeeds.
    await reserve_pending(_TWILIO, _HUMAN, "https://app.example/next", "int-2", _deadline())


# -- the MessageSid dedupe set (upstream replay guard) ---------------------------


async def test_seen_round_trip_uses_dedupe_ttl(fake_redis: FakeRedis):
    assert await already_seen("SM123") is False

    await mark_seen("SM123")

    assert await already_seen("SM123") is True
    assert fake_redis.ttls["channel:twilio:seen:SM123"] == 172_800


async def test_missing_redis_url_raises_on_every_store_function(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_REDIS_URL")
    reset_all_settings()

    for call in (
        twilio_correlation_store.set_correlation(_KEY, _entry(), ttl_seconds=300),
        twilio_correlation_store.get_correlation(_KEY),
        twilio_correlation_store.release_correlation(_KEY),
        reserve_pending(_TWILIO, _HUMAN, _CALLBACK, "int-1", _deadline()),
        release_pending(_TWILIO, _HUMAN),
        already_seen("SM123"),
        mark_seen("SM123"),
    ):
        with pytest.raises(ValueError, match="CHANNEL_TWILIO_REDIS_URL"):
            await call
