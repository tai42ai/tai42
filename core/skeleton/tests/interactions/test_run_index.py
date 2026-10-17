"""The per-run park index and the kill mark: a park joins its run's index, a kill marks the run dead.

The kill reads the index to revoke every park of the run; the mark is what refuses a new park and a
resume of a dead run. The park's add writes the index, both prunes leave it, and its TTL outlives
the parks it indexes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse

from tai42_skeleton.interactions.store import InteractionStore, KillDue
from tai42_skeleton.interactions.store.writes_kills import KILL_ACT_ON_ANY

_RID = "rd-ri"


def _request(store: InteractionStore, iid: str, *, run_delivery_id: str | None = _RID) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id="gr",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(hours=1),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=now + timedelta(hours=1),
        run_delivery_id=run_delivery_id,
    )


async def _add(fake_redis, store: InteractionStore, iid: str, *, run_delivery_id: str | None = _RID) -> None:
    await store.add(
        fake_redis, _request(store, iid, run_delivery_id=run_delivery_id), idle_ttl=100, run_delivery_id=run_delivery_id
    )


async def _enqueue_kill(fake_redis, store: InteractionStore, iid: str, *, kill_due_ttl: int = 500) -> str:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return await store.enqueue_kill(
        fake_redis,
        iid,
        "gr",
        delivery=None,
        run_delivery_id=_RID,
        subjects=None,
        chain_keys=[],
        reason="cancelled",
        kill_due_ttl=kill_due_ttl,
        first_attempt_at_ms=now_ms,
        deadline_ms=now_ms + 100_000,
        act_on=KILL_ACT_ON_ANY,
        continuation_tool="resume_tool",
    )


async def test_a_park_joins_its_runs_index_and_the_index_outlives_it(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")
    await _add(fake_redis, store, "b1")

    assert await store.run_members(fake_redis, _RID) == ["a1", "b1"]
    assert await fake_redis.ttl(store.run_parks_key(_RID)) >= await fake_redis.ttl(store.state_key("a1"))


async def test_a_run_less_park_joins_no_run_index(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "s1", run_delivery_id=None)
    assert await store.run_members(fake_redis, _RID) == []


async def test_prune_pending_leaves_the_run_index(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")
    await _add(fake_redis, store, "b1")

    assert await store.prune_pending(fake_redis, "a1", "gr", reason="cancelled") == "pruned"

    assert await store.run_members(fake_redis, _RID) == ["b1"]


async def test_a_kill_leaves_the_run_index_and_marks_the_run_once(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")
    await _add(fake_redis, store, "b1")
    assert not await store.run_killed(fake_redis, _RID)

    assert await _enqueue_kill(fake_redis, store, "a1", kill_due_ttl=500) == "pruned"

    assert await store.run_members(fake_redis, _RID) == ["b1"]
    assert await store.run_killed(fake_redis, _RID)
    assert await fake_redis.get(store.run_killed_key(_RID)) == "cancelled"
    assert 0 < await fake_redis.ttl(store.run_killed_key(_RID)) <= 500
    # A second kill of the run leaves the first mark as it stands (NX).
    fake_redis.time += 10
    await _enqueue_kill(fake_redis, store, "b1", kill_due_ttl=5000)
    assert await fake_redis.ttl(store.run_killed_key(_RID)) <= 490
    assert await store.run_members(fake_redis, _RID) == []


async def test_a_kill_of_an_answered_park_leaves_the_run_index(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")
    now = datetime.now(UTC)
    await store.record_answer(
        fake_redis,
        InteractionResponse(interaction_id="a1", answer="y", answered_by="op", answered_at=now),
        "gr",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )
    assert await store.run_members(fake_redis, _RID) == ["a1"]

    assert await _enqueue_kill(fake_redis, store, "a1") == "answered"

    assert await store.run_members(fake_redis, _RID) == []
    assert await store.run_killed(fake_redis, _RID)


async def test_a_park_of_a_dead_run_is_refused_and_nothing_is_written(fake_redis):
    from tai42_contract.interactions import ParkRunTakenDownError

    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")
    await _enqueue_kill(fake_redis, store, "a1")

    with pytest.raises(ParkRunTakenDownError, match="p2"):
        await _add(fake_redis, store, "p2")

    assert await store.get_state(fake_redis, "p2") is None
    assert await store.run_members(fake_redis, _RID) == []
    assert await fake_redis.zrangebyscore(store.open_key, 0, 2**63) == []


async def test_the_kill_target_and_its_record_carry_the_continuation_tool(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")

    target = await store.read_kill_target(fake_redis, "a1")
    assert target is not None
    assert target.continuation_tool == "resume_tool"

    await _enqueue_kill(fake_redis, store, "a1")
    due = await store.claim_kill_retry(fake_redis, "a1", datetime.now(UTC) + timedelta(hours=1), 1000, 60_000)
    assert isinstance(due, KillDue)
    assert due.continuation_tool == "resume_tool"


async def test_the_kill_target_of_a_running_entry_reads_the_tool_off_its_due_record(fake_redis):
    store = InteractionStore("t:")
    await _add(fake_redis, store, "a1")
    now = datetime.now(UTC)
    await store.record_answer(
        fake_redis,
        InteractionResponse(interaction_id="a1", answer="y", answered_by="op", answered_at=now),
        "gr",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )
    await fake_redis.delete(store.state_key("a1"))

    target = await store.read_kill_target(fake_redis, "a1")
    assert target is not None
    assert target.kind == "running"
    assert target.continuation_tool == "resume_tool"
