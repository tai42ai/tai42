"""A person merge that reaches a record as it leaves.

The merge re-keys every stored subject copy of a moved entry: the state hash, the
continuation-due record, the kill-due record and the outcome hash. Each copy is read and
then rewritten; when the record lapses through its TTL, or is deleted, between that read
and that write, the record is gone and the merge must leave it gone, never write a
partial hash with no TTL in its place.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest
from tai42_contract.states import StateContext, SubjectCandidates

from tai42_skeleton.interactions import InteractionStore

_IID = "m1"
_CANDIDATES = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA"})
_CONTEXT = StateContext(door="conversation", candidates=_CANDIDATES)
_DESCRIPTOR = {"target_kind": "agent", "target_name": "a", "by_kind": {"person": "pA"}}


async def _index(fake_redis, store: InteractionStore, member: str) -> None:
    await fake_redis.sadd(store.subject_parks_key("agent", "a", "person", "pA"), member)
    await fake_redis.sadd(store.subject_scopes_key("person", "pA"), "agent:a")


async def _state(fake_redis, store: InteractionStore) -> str:
    now = datetime.now(UTC)
    request = InteractionRequest(
        interaction_id=_IID,
        group_id="g1",
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(_IID),
        created_at=now,
        timeout_at=now + timedelta(minutes=5),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=_CONTEXT,
        expiry_at=now + timedelta(minutes=5),
    )
    await store.add(fake_redis, request, idle_ttl=60, to="caller")
    return store.state_key(_IID)


async def _continuation_due(fake_redis, store: InteractionStore) -> str:
    key = store.continuation_due_key(_IID)
    await fake_redis.hset(key, mapping={"tool": "resume_tool", "state_context": _CONTEXT.model_dump_json()})
    await fake_redis.expire(key, 60)
    await _index(fake_redis, store, _IID)
    return key


async def _kill_due(fake_redis, store: InteractionStore) -> str:
    key = store.kill_due_key(_IID)
    await fake_redis.hset(key, mapping={"reason": "erased", "subjects": json.dumps(_DESCRIPTOR)})
    await fake_redis.expire(key, 60)
    await _index(fake_redis, store, _IID)
    return key


async def _outcome(fake_redis, store: InteractionStore) -> str:
    await store.add_outcome(
        fake_redis,
        completion_id=_IID,
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=_CANDIDATES,
        retention_ttl=60,
    )
    return store.outcome_key(_IID)


_RECORDS: dict[str, Callable[..., Awaitable[str]]] = {
    "state": _state,
    "continuation-due": _continuation_due,
    "kill-due": _kill_due,
    "outcome": _outcome,
}


@pytest.mark.parametrize("record", list(_RECORDS))
@pytest.mark.parametrize("leaves", ["lapses", "is deleted"])
async def test_a_merge_never_recreates_a_record_that_left_after_it_was_read(fake_redis, record, leaves):
    store = InteractionStore("rekey:")
    key = await _RECORDS[record](fake_redis, store)
    assert await fake_redis.ttl(key) > 0
    read_all = fake_redis._hgetall
    left = False

    def _hgetall(name):
        nonlocal left
        fields = read_all(name)
        if name == key and fields and not left:
            left = True
            if leaves == "lapses":
                fake_redis.advance(10**7)
            else:
                fake_redis._delete(key)
        return fields

    fake_redis._hgetall = _hgetall

    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")

    assert left
    assert await fake_redis.exists(key) == 0


def _old_sets(store: InteractionStore) -> dict[str, str]:
    return {
        "scopes": store.subject_scopes_key("person", "pA"),
        "parks": store.subject_parks_key("agent", "a", "person", "pA"),
    }


def _new_sets(store: InteractionStore) -> list[str]:
    return [store.subject_scopes_key("person", "pB"), store.subject_parks_key("agent", "a", "person", "pB")]


async def _indexed_sets(fake_redis, store: InteractionStore) -> None:
    old = _old_sets(store)
    await fake_redis.sadd(old["parks"], _IID)
    await fake_redis.expire(old["parks"], 50)
    await fake_redis.sadd(old["scopes"], "agent:a")
    await fake_redis.expire(old["scopes"], 60)


async def test_a_merge_carries_each_index_set_horizon_onto_the_new_sets(fake_redis):
    store = InteractionStore("rekey:")
    await _indexed_sets(fake_redis, store)

    assert await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB") == [_IID]

    new_scopes, new_parks = _new_sets(store)
    assert await fake_redis.smembers(new_scopes) == {"agent:a"}
    assert await fake_redis.smembers(new_parks) == {_IID}
    assert await fake_redis.ttl(new_scopes) == 60
    assert await fake_redis.ttl(new_parks) == 50


async def test_a_merge_carries_a_sub_second_index_set_horizon_onto_the_new_sets(fake_redis):
    # Under half a second left, a whole-second TTL reads 0 while the old sets still exist; the
    # merge still moves them, so the new sets carry the exact remaining horizon.
    store = InteractionStore("rekey:")
    old = _old_sets(store)
    await fake_redis.sadd(old["parks"], _IID)
    await fake_redis.pexpire(old["parks"], 400)
    await fake_redis.sadd(old["scopes"], "agent:a")
    await fake_redis.pexpire(old["scopes"], 300)
    assert await fake_redis.ttl(old["parks"]) == 0

    assert await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB") == [_IID]

    new_scopes, new_parks = _new_sets(store)
    assert await fake_redis.smembers(new_parks) == {_IID}
    assert await fake_redis.pttl(new_scopes) == 300
    assert await fake_redis.pttl(new_parks) == 400


@pytest.mark.parametrize("index_set", ["scopes", "parks"])
async def test_a_merge_never_writes_a_new_index_set_with_no_ttl_when_the_old_one_lapses_after_its_read(
    fake_redis, index_set
):
    store = InteractionStore("rekey:")
    await _indexed_sets(fake_redis, store)
    target = _old_sets(store)[index_set]
    read_members = fake_redis._smembers
    lapsed = False

    def _smembers(name):
        nonlocal lapsed
        members = read_members(name)
        if name == target and members and not lapsed:
            lapsed = True
            fake_redis.advance(10**7)
        return members

    fake_redis._smembers = _smembers

    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")

    assert lapsed
    for key in _new_sets(store):
        assert await fake_redis.exists(key) == 0 or await fake_redis.ttl(key) > 0, key


@pytest.mark.parametrize("index_set", ["scopes", "parks"])
async def test_a_merge_gives_an_index_set_rewritten_between_its_reads_its_horizon(fake_redis, index_set):
    store = InteractionStore("rekey:")
    await _indexed_sets(fake_redis, store)
    old = _old_sets(store)
    target = old[index_set]
    read_pttl = fake_redis._pttl
    rewritten = False

    def _pttl(name):
        nonlocal rewritten
        if name != target or rewritten:
            return read_pttl(name)
        rewritten = True
        # The old set lapses just before the merge reads its TTL, and a park joins the old
        # subject again right after, before the merge reads the set's members.
        fake_redis._delete(target)
        remaining = read_pttl(name)
        fake_redis._sadd(old["parks"], _IID)
        fake_redis._expire(old["parks"], 50)
        fake_redis._sadd(old["scopes"], "agent:a")
        fake_redis._expire(old["scopes"], 60)
        return remaining

    fake_redis._pttl = _pttl

    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")

    assert rewritten
    for key in _new_sets(store):
        assert await fake_redis.exists(key) == 0 or await fake_redis.ttl(key) > 0, key
