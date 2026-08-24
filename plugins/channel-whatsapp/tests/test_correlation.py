"""The WhatsApp correlation store — the contract port + the adapter-private rich
record (options/schema/question/rejections), plus the flow-id cache, dedupe set, and
known-contact marker.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError, Correlation

from tai42_channel_whatsapp.correlation import (
    PendingQuestion,
    PendingQuestionExistsError,
    already_seen,
    bump_rejections,
    cache_flow_id,
    correlation_key,
    get_cached_flow_id,
    is_known_contact,
    mark_known_contact,
    mark_seen,
    peek_pending,
    release_pending,
    reserve_pending,
    whatsapp_correlation_store,
)

from .conftest import FakeRedis

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_PNID = "10000000000001"
_WA = "15559990001"
_OKEY = correlation_key(_PNID, _WA)
_KEY = f"channel:whatsapp:pending:{_OKEY}"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _corr(callback_url: str = _CALLBACK, interaction_id: str = "int-1") -> Correlation:
    return Correlation(callback_url=callback_url, interaction_id=interaction_id, ttl_deadline=_deadline())


# -- the contract port: set (NX) / get (peek, port fields) / release -------------


async def test_port_set_get_round_trips_port_fields(fake_redis: FakeRedis):
    stored = await whatsapp_correlation_store.set_correlation(_OKEY, _corr(interaction_id="int-9"), ttl_seconds=300)
    assert stored is True
    got = await whatsapp_correlation_store.get_correlation(_OKEY)
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-9"
    # Non-destructive peek: the reservation survives.
    assert await whatsapp_correlation_store.get_correlation(_OKEY) is not None


async def test_port_get_unknown_returns_none(fake_redis: FakeRedis):
    assert await whatsapp_correlation_store.get_correlation(_OKEY) is None


async def test_port_set_is_nx_one_pending_per_pair(fake_redis: FakeRedis):
    first = await whatsapp_correlation_store.set_correlation(_OKEY, _corr(interaction_id="first"), ttl_seconds=300)
    assert first is True
    second = await whatsapp_correlation_store.set_correlation(_OKEY, _corr(interaction_id="second"), ttl_seconds=300)
    assert second is False
    held = await whatsapp_correlation_store.get_correlation(_OKEY)
    assert held is not None
    assert held.interaction_id == "first"


async def test_port_release_is_idempotent(fake_redis: FakeRedis):
    await whatsapp_correlation_store.set_correlation(_OKEY, _corr(), ttl_seconds=300)
    await whatsapp_correlation_store.release_correlation(_OKEY)
    assert not fake_redis.store
    await whatsapp_correlation_store.release_correlation(_OKEY)  # no-op, never an error


async def test_ladder_peek_reads_the_rich_reserve_record(fake_redis: FakeRedis):
    # The ladder's port get projects the SAME record reserve_pending wrote down to the
    # three port fields, so a form ask reserved with schema is still forwardable.
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline(), interaction_id="int-9", schema=schema, question="Q?")
    got = await whatsapp_correlation_store.get_correlation(_OKEY)
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-9"


# -- the deliver-path front door + adapter-private rich peek ---------------------


async def test_reserve_peek_round_trip(fake_redis: FakeRedis):
    timeout_at = _deadline()
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at, interaction_id="int-1")

    peeked = await peek_pending(_PNID, _WA)

    assert peeked is not None
    assert peeked.callback_url == _CALLBACK
    assert peeked.interaction_id == "int-1"
    # A peek is non-destructive — the record survives a second read.
    assert await peek_pending(_PNID, _WA) is not None


async def test_reservation_ttl_is_remaining_budget(fake_redis: FakeRedis):
    before = datetime.now(UTC)
    timeout_at = _deadline(120)
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at, interaction_id="int-1")
    after = datetime.now(UTC)

    ttl = fake_redis.ttls[_KEY]
    assert math.ceil((timeout_at - after).total_seconds()) <= ttl <= math.ceil((timeout_at - before).total_seconds())


async def test_double_reserve_rejected(fake_redis: FakeRedis):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline(), interaction_id="int-1")

    with pytest.raises(PendingQuestionExistsError, match="already pending"):
        await reserve_pending(_PNID, _WA, "https://app.example/other", _deadline(), interaction_id="int-2")


async def test_reserve_past_deadline_raises_and_stores_nothing(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="already passed"):
        await reserve_pending(_PNID, _WA, _CALLBACK, _deadline(-1), interaction_id="int-1")

    assert not fake_redis.store  # nothing reserved for an expired budget


async def test_release_frees_the_pair(fake_redis: FakeRedis):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline(), interaction_id="int-1")
    await release_pending(_PNID, _WA)

    await reserve_pending(_PNID, _WA, "https://app.example/next", _deadline(), interaction_id="int-2")


async def test_select_pending_round_trips_options_and_interaction_id(fake_redis: FakeRedis):
    timeout_at = _deadline()
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at, options=["staging", "production"], interaction_id="int-9")

    peeked = await peek_pending(_PNID, _WA)

    assert peeked == PendingQuestion(
        callback_url=_CALLBACK, timeout_at=timeout_at, options=["staging", "production"], interaction_id="int-9"
    )


async def test_text_pending_has_no_options_or_schema(fake_redis: FakeRedis):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline(), interaction_id="int-1")

    peeked = await peek_pending(_PNID, _WA)

    assert peeked is not None
    assert peeked.options is None
    assert peeked.schema is None


async def test_form_pending_round_trips_schema(fake_redis: FakeRedis):
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    timeout_at = _deadline()
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at, interaction_id="int-9", schema=schema, question="Q?")

    peeked = await peek_pending(_PNID, _WA)

    assert peeked == PendingQuestion(
        callback_url=_CALLBACK, timeout_at=timeout_at, interaction_id="int-9", schema=schema, question="Q?"
    )


# -- bump_rejections: the in-place counter on the still-held record --------------


async def test_bump_rejections_increments_and_preserves_the_record(fake_redis: FakeRedis):
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    timeout_at = _deadline(600)
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at, interaction_id="int-9", schema=schema, question="Q?")
    held = await peek_pending(_PNID, _WA)
    assert held is not None
    assert held.rejections == 0

    before = datetime.now(UTC)
    await bump_rejections(_PNID, _WA, held)
    after = datetime.now(UTC)

    updated = await peek_pending(_PNID, _WA)
    assert updated is not None
    assert updated.rejections == 1
    # Everything else survives, and the TTL stays the remaining budget.
    assert updated.interaction_id == "int-9"
    assert updated.schema == schema
    assert updated.question == "Q?"
    ttl = fake_redis.ttls[_KEY]
    assert math.ceil((timeout_at - after).total_seconds()) <= ttl <= math.ceil((timeout_at - before).total_seconds())


async def test_bump_rejections_past_deadline_writes_nothing(fake_redis: FakeRedis):
    stale = PendingQuestion(callback_url=_CALLBACK, timeout_at=datetime.now(UTC) - timedelta(seconds=1))
    await bump_rejections(_PNID, _WA, stale)
    assert not fake_redis.store


# -- the dedupe set, flow-id cache, known-contact marker (unchanged families) ----


async def test_seen_round_trip_uses_dedupe_ttl(fake_redis: FakeRedis):
    assert await already_seen("wamid.ABC") is False

    await mark_seen("wamid.ABC")

    assert await already_seen("wamid.ABC") is True
    assert fake_redis.ttls["channel:whatsapp:seen:wamid.ABC"] == 172_800


async def test_flow_id_cache_round_trip_has_no_ttl(fake_redis: FakeRedis):
    assert await get_cached_flow_id("WABA-1", "hash-abc") is None

    await cache_flow_id("WABA-1", "hash-abc", "flow-77")

    assert await get_cached_flow_id("WABA-1", "hash-abc") == "flow-77"
    key = "channel:whatsapp:flow:WABA-1:hash-abc"
    assert key in fake_redis.store
    assert key not in fake_redis.ttls  # a published Flow persists on Meta — no TTL


async def test_known_contact_marker_round_trip_uses_window_ttl(fake_redis: FakeRedis):
    assert await is_known_contact(_PNID, _WA) is False

    await mark_known_contact(_PNID, _WA)

    assert await is_known_contact(_PNID, _WA) is True
    assert fake_redis.ttls[f"channel:whatsapp:known-contact:{_PNID}:{_WA}"] == 30 * 86_400


async def test_known_contact_window_zero_writes_nothing(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_TEMPLATE_CONTACT_WINDOW_DAYS", "0")
    reset_all_settings()

    await mark_known_contact(_PNID, _WA)

    assert not fake_redis.store  # allowlist-only mode: no marker
    assert await is_known_contact(_PNID, _WA) is False


async def test_missing_redis_url_raises_on_every_store_function(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_REDIS_URL")
    reset_all_settings()

    question = PendingQuestion(callback_url=_CALLBACK, timeout_at=_deadline(), interaction_id="int-1")
    for call in (
        whatsapp_correlation_store.set_correlation(_OKEY, _corr(), ttl_seconds=300),
        whatsapp_correlation_store.get_correlation(_OKEY),
        whatsapp_correlation_store.release_correlation(_OKEY),
        reserve_pending(_PNID, _WA, _CALLBACK, _deadline(), interaction_id="int-1"),
        release_pending(_PNID, _WA),
        peek_pending(_PNID, _WA),
        bump_rejections(_PNID, _WA, question),
        already_seen("wamid.ABC"),
        mark_seen("wamid.ABC"),
        mark_known_contact(_PNID, _WA),
        is_known_contact(_PNID, _WA),
        get_cached_flow_id("WABA-1", "hash-abc"),
        cache_flow_id("WABA-1", "hash-abc", "flow-1"),
    ):
        with pytest.raises(ValueError, match="CHANNEL_WHATSAPP_REDIS_URL"):
            await call
