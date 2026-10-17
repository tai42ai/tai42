"""A claim that reaches a park at its TTL edge.

A one-park group's state hash and pending count share one horizon, so they lapse
together. Each claim (kill, answer, prune) reads the state and the count in separate
round trips under WATCH; when the shared horizon passes between those reads, or in the
middle of a claim's reads of the state, the park has simply expired, and the claim must
see it as gone, never as a torn index or a torn record. A pending count that is gone while
the state is live is a torn index, and each claim raises on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
)

from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.interactions.store.writes_kills import KILL_ACT_ON_PENDING

_GROUP = "edge"
_IID = "p1"


def _request(store: InteractionStore) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=_IID,
        group_id=_GROUP,
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(_IID),
        created_at=now,
        timeout_at=now + timedelta(minutes=5),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=now + timedelta(minutes=5),
        on_expiry="kill",
        run_delivery_id="rd-edge",
    )


async def _park_lapsing_after_reading(fake_redis, field: str) -> InteractionStore:
    """Park one ask in its own group; the shared horizon passes right after ``field`` of its state is read."""
    store = InteractionStore("edge:")
    await store.add(fake_redis, _request(store), idle_ttl=60, run_delivery_id="rd-edge")
    state_key = store.state_key(_IID)
    assert await fake_redis.get(store.count_key(_GROUP)) == "1"
    read_field, read_fields = fake_redis._hget, fake_redis._hmget
    lapsed = False

    def _lapse() -> None:
        nonlocal lapsed
        if not lapsed:
            lapsed = True
            fake_redis.advance(10**7)

    def _hget(key, name):
        value = read_field(key, name)
        if key == state_key and name == field:
            _lapse()
        return value

    def _hmget(key, keys):
        values = read_fields(key, keys)
        if key == state_key and field in keys:
            _lapse()
        return values

    fake_redis._hget = _hget
    fake_redis._hmget = _hmget
    return store


async def test_an_expiry_kill_at_the_ttl_edge_finds_the_park_gone(fake_redis):
    store = await _park_lapsing_after_reading(fake_redis, "status")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    result = await store.enqueue_kill(
        fake_redis,
        _IID,
        _GROUP,
        delivery=None,
        run_delivery_id="rd-edge",
        subjects=None,
        chain_keys=[],
        reason="expired",
        terminal=PARK_COMPLETION_FAILED,
        kill_due_ttl=500,
        first_attempt_at_ms=now_ms,
        deadline_ms=now_ms + 100_000,
        act_on=KILL_ACT_ON_PENDING,
        continuation_tool="resume_tool",
    )

    assert result == "gone"
    assert await fake_redis.hgetall(store.kill_due_key(_IID)) == {}


async def test_an_answer_at_the_ttl_edge_finds_the_park_gone(fake_redis):
    store = await _park_lapsing_after_reading(fake_redis, "status")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    response = InteractionResponse(interaction_id=_IID, answer="x", answered_at=datetime.now(UTC), answered_by="alice")

    claimed = await store.record_answer(
        fake_redis,
        response,
        _GROUP,
        reply_ttl=5,
        continuation_due_ttl=60,
        continuation_first_attempt_at_ms=now_ms,
    )

    assert claimed is False
    assert await fake_redis.hgetall(store.continuation_due_key(_IID)) == {}


async def test_an_answer_whose_park_lapses_mid_read_finds_the_park_gone(fake_redis):
    store = await _park_lapsing_after_reading(fake_redis, "continuation_tool")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    response = InteractionResponse(interaction_id=_IID, answer="x", answered_at=datetime.now(UTC), answered_by="alice")

    claimed = await store.record_answer(
        fake_redis,
        response,
        _GROUP,
        reply_ttl=5,
        continuation_due_ttl=60,
        continuation_first_attempt_at_ms=now_ms,
    )

    assert claimed is False
    assert await fake_redis.hgetall(store.continuation_due_key(_IID)) == {}


async def test_a_prune_at_the_ttl_edge_finds_the_park_gone(fake_redis):
    store = await _park_lapsing_after_reading(fake_redis, "status")

    assert await store.prune_pending(fake_redis, _IID, _GROUP, reason="cancelled") == "gone"


async def _park_with_its_count_deleted(fake_redis) -> InteractionStore:
    """Park one ask in its own group, then delete the group's pending count while the state stays live."""
    store = InteractionStore("edge:")
    await store.add(fake_redis, _request(store), idle_ttl=60, run_delivery_id="rd-edge")
    assert await fake_redis.delete(store.count_key(_GROUP)) == 1
    assert await fake_redis.hget(store.state_key(_IID), "status") == "pending"
    return store


async def test_a_kill_of_a_live_park_with_no_pending_count_raises(fake_redis):
    store = await _park_with_its_count_deleted(fake_redis)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    with pytest.raises(RuntimeError, match="pending count missing"):
        await store.enqueue_kill(
            fake_redis,
            _IID,
            _GROUP,
            delivery=None,
            run_delivery_id="rd-edge",
            subjects=None,
            chain_keys=[],
            reason="expired",
            terminal=PARK_COMPLETION_FAILED,
            kill_due_ttl=500,
            first_attempt_at_ms=now_ms,
            deadline_ms=now_ms + 100_000,
            act_on=KILL_ACT_ON_PENDING,
            continuation_tool="resume_tool",
        )


async def test_an_answer_to_a_live_park_with_no_pending_count_raises(fake_redis):
    store = await _park_with_its_count_deleted(fake_redis)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    response = InteractionResponse(interaction_id=_IID, answer="x", answered_at=datetime.now(UTC), answered_by="alice")

    with pytest.raises(RuntimeError, match="pending count missing"):
        await store.record_answer(
            fake_redis,
            response,
            _GROUP,
            reply_ttl=5,
            continuation_due_ttl=60,
            continuation_first_attempt_at_ms=now_ms,
        )


async def test_a_prune_of_a_live_park_with_no_pending_count_raises(fake_redis):
    store = await _park_with_its_count_deleted(fake_redis)

    with pytest.raises(RuntimeError, match="pending count missing"):
        await store.prune_pending(fake_redis, _IID, _GROUP, reason="cancelled")
