"""The subject index, the second (caller) concurrency cap, and the waiting-outcome store.

Every async park that carries a state context joins ``subject-parks:{scope}:{kind}:{key}`` once
per ``candidates.by_kind`` entry — user asks and caller asks alike — plus the ``subject-scopes``
set that lets an erasure/merge find every scope without a SCAN. ``list_parked_for`` unions the
run's subject keys, de-duplicates by id, is scope-exact, and tags each entry ``asking`` /
``running`` / ``finished`` / ``failed``. The membership leaves wherever the backing record leaves
(prune, kill teardown, outcome take/delete) and is KEPT across an answer (``asking`` →
``running``). The caller cap (``open:caller``) is independent of the user cap (``open``). A person
merge re-keys the index and the stored subject. And the continuation-due record carries the run's
``delivery`` + ``run_delivery_id`` into a detached redelivery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse
from tai42_contract.states import StateContext, SubjectCandidates

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions.store import ContinuationDue, KillDue


def _candidates(target_name: str = "a", **by_kind: str) -> SubjectCandidates:
    return SubjectCandidates(target_kind="agent", target_name=target_name, by_kind=dict(by_kind))


def _context(candidates: SubjectCandidates) -> StateContext:
    return StateContext(door="conversation", candidates=candidates)


def _park(
    store: InteractionStore,
    iid: str,
    *,
    gid: str = "g1",
    candidates: SubjectCandidates | None = None,
    expiry_minutes: int = 60,
) -> InteractionRequest:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=expiry_minutes)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=_context(candidates) if candidates is not None else None,
        expiry_at=expiry,
    )


def _answer(iid: str) -> InteractionResponse:
    return InteractionResponse(interaction_id=iid, answer="ok", answered_by="op", answered_at=datetime.now(UTC))


# -- join --------------------------------------------------------------------


async def test_add_joins_the_subject_index_per_by_kind_key_and_scope(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA", thread="bridge:chat:+1555")
    await store.add(fake_redis, _park(store, "i1", candidates=cands), idle_ttl=86400, to="caller")
    # One member under each subject key, under this run's scope.
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == {"i1"}
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "thread", "bridge:chat:+1555")) == {"i1"}
    # The scope is recorded per (kind, key) so an erasure/merge finds it.
    assert await fake_redis.smembers(store.subject_scopes_key("person", "pA")) == {"agent:a"}
    # ``to`` is denormalized only for a caller ask.
    assert await fake_redis.hget(store.state_key("i1"), "to") == "caller"


async def test_user_ask_joins_the_index_too(fake_redis):
    store = InteractionStore("t:")
    await store.add(fake_redis, _park(store, "u1", candidates=_candidates(person="pU")), idle_ttl=86400)
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pU")) == {"u1"}
    # A user ask stamps no ``to`` marker (absent means user).
    assert await fake_redis.hget(store.state_key("u1"), "to") is None


async def test_park_without_context_joins_nothing(fake_redis):
    store = InteractionStore("t:")
    await store.add(fake_redis, _park(store, "n1"), idle_ttl=86400)
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()


async def test_colon_in_a_thread_key_never_collides_two_subjects(fake_redis):
    store = InteractionStore("t:")
    # Two distinct subjects whose naive ``:``-joined keys would collide.
    await store.add(fake_redis, _park(store, "a1", candidates=_candidates(thread="x:y")), idle_ttl=86400)
    await store.add(
        fake_redis, _park(store, "b1", gid="g2", candidates=_candidates(target_name="x", thread="y")), idle_ttl=86400
    )
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "thread", "x:y")) == {"a1"}
    assert await fake_redis.smembers(store.subject_parks_key("agent", "x", "thread", "y")) == {"b1"}


# -- leave on every delete path ----------------------------------------------


async def test_prune_leaves_the_index(fake_redis):
    store = InteractionStore("t:")
    await store.add(fake_redis, _park(store, "i1", candidates=_candidates(person="pA")), idle_ttl=86400)
    assert await store.prune_pending(fake_redis, "i1", "g1") == "pruned"
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()


async def test_kill_teardown_leaves_the_index(fake_redis):
    # The whole-chain kill's teardown MULTI drops the killed park from every subject-parks set it
    # joined, exactly where the state key is deleted.
    store = InteractionStore("t:")
    req = _park(store, "i1", candidates=_candidates(person="pA"))
    await store.add(fake_redis, req, idle_ttl=86400, thread_id="th")
    target = await store.read_kill_target(fake_redis, "i1")
    assert target is not None
    assert (
        await store.enqueue_kill(
            fake_redis,
            "i1",
            "g1",
            delivery=target.delivery,
            run_delivery_id=target.run_delivery_id,
            subjects=target.subjects,
            reason="thread_deleted",
            kill_due_ttl=86400,
            first_attempt_at_ms=0,
            deadline_ms=0,
        )
        == "pruned"
    )
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()


async def test_answer_keeps_the_index_member_as_running(fake_redis):
    store = InteractionStore("t:")
    await store.add(fake_redis, _park(store, "i1", candidates=_candidates(person="pA")), idle_ttl=86400, to="caller")
    claimed = await store.record_answer(
        fake_redis,
        _answer("i1"),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    assert claimed is True
    # The entry moves asking -> running: the subject member is KEPT while the
    # continuation-due record stands, and the caller open slot is freed.
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == {"i1"}
    assert await store.count_open(fake_redis) == 0


# -- the caller cap is independent of the user cap ---------------------------


async def test_caller_and_user_caps_are_independent(fake_redis):
    store = InteractionStore("t:")
    user_req = _park(store, "u1", candidates=_candidates(person="pU"))
    caller_req = _park(store, "c1", gid="g2", candidates=_candidates(person="pC"))
    # Fill the user cap (limit 1).
    assert await store.reserve_open_slot(fake_redis, user_req, 1, to="user") is True
    assert await store.reserve_open_slot(fake_redis, _park(store, "u2", gid="g3"), 1, to="user") is False
    # The caller cap is a separate index — a full user cap does not refuse it.
    assert await store.reserve_open_slot(fake_redis, caller_req, 1, to="caller") is True
    assert await store.reserve_open_slot(fake_redis, _park(store, "c2", gid="g4"), 1, to="caller") is False
    # ...and the members landed in the right zsets.
    assert await fake_redis.zcard(store.open_key) == 1
    assert await fake_redis.zcard(store.open_caller_key) == 1


async def test_answer_frees_the_caller_slot_not_the_user_slot(fake_redis):
    store = InteractionStore("t:")
    caller = _park(store, "c1", candidates=_candidates(person="pC"))
    assert await store.reserve_open_slot(fake_redis, caller, 10, to="caller") is True
    await store.add(fake_redis, caller, idle_ttl=86400, open_member_reserved=True, to="caller")
    assert await fake_redis.zcard(store.open_caller_key) == 1
    await store.record_answer(
        fake_redis,
        _answer("c1"),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    assert await fake_redis.zcard(store.open_caller_key) == 0
    assert await fake_redis.zcard(store.open_key) == 0


# -- list_parked_for: union, de-dup, scope-exact, status ---------------------


async def test_list_parked_for_unions_and_dedups(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA", thread="th")
    await store.add(fake_redis, _park(store, "i1", candidates=cands), idle_ttl=86400, to="caller")
    # A second park sharing only the person key.
    await store.add(
        fake_redis, _park(store, "i2", gid="g2", candidates=_candidates(person="pA")), idle_ttl=86400, to="caller"
    )
    entries = await store.list_parked_for(fake_redis, cands)
    ids = sorted(e["id"] for e in entries)
    assert ids == ["i1", "i2"]  # union over person+thread keys, i1 de-duped across both
    assert all(e["status"] == "asking" for e in entries)


async def test_list_parked_for_is_scope_exact(fake_redis):
    store = InteractionStore("t:")
    # Same (kind, key) under two different scopes.
    await store.add(
        fake_redis, _park(store, "i1", candidates=_candidates(target_name="a", person="pA")), idle_ttl=86400
    )
    await store.add(
        fake_redis, _park(store, "i2", gid="g2", candidates=_candidates(target_name="b", person="pA")), idle_ttl=86400
    )
    only_a = await store.list_parked_for(fake_redis, _candidates(target_name="a", person="pA"))
    assert [e["id"] for e in only_a] == ["i1"]


async def test_list_parked_for_reports_running_and_finished(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add(fake_redis, _park(store, "i1", candidates=cands), idle_ttl=86400, to="caller")
    await store.record_answer(
        fake_redis,
        _answer("i1"),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    running = await store.list_parked_for(fake_redis, cands)
    assert [(e["id"], e["status"]) for e in running] == [("i1", "running")]
    # The run terminates: a waiting outcome replaces the running entry.
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="finished",
        result={"v": 1},
        candidates=cands,
        retention_ttl=3600,
    )
    finished = await store.list_parked_for(fake_redis, cands)
    assert [(e["id"], e["status"], e["result"]) for e in finished] == [("cid", "finished", {"v": 1})]


# -- waiting outcomes --------------------------------------------------------


async def test_add_outcome_is_idempotent_by_completion_id(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
    )
    # A redelivery / a buffered sibling re-driving the same terminal is a no-op.
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i2",
        status="finished",
        result=999,
        candidates=cands,
        retention_ttl=3600,
    )
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == {"cid"}
    outcome = await store.claim_outcome(fake_redis, "cid")
    assert outcome is not None
    assert outcome.status == "finished"
    assert outcome.result == 1


async def test_add_outcome_joins_the_retention_index_and_carries_run_identity(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
        run_delivery_id="rd-1",
    )
    # The row joined the retention index and carries the run identity the sweep event names.
    assert await fake_redis.zrangebyscore(store.outcome_retention_index_key, 0, 10**18) == ["cid"]
    outcome = await store.claim_outcome(fake_redis, "cid")
    assert outcome is not None
    assert outcome.interaction_id == "i1"
    assert outcome.run_delivery_id == "rd-1"
    # Taken -> dropped from the retention index alongside the subject/caller indexes.
    assert await fake_redis.zrangebyscore(store.outcome_retention_index_key, 0, 10**18) == []


async def test_due_untaken_outcomes_lists_only_members_aged_past_the_horizon(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add_outcome(
        fake_redis,
        completion_id="fresh",
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
    )
    # A just-written outcome is inside the horizon.
    assert await store.due_untaken_outcomes(fake_redis, datetime.now(UTC), 3600) == []
    # Age its index member past the horizon.
    await fake_redis.zadd(store.outcome_retention_index_key, {"fresh": 0})
    assert await store.due_untaken_outcomes(fake_redis, datetime.now(UTC), 3600) == ["fresh"]
    # A take drops the member, so the sweep no longer lists it.
    await store.claim_outcome(fake_redis, "fresh")
    assert await store.due_untaken_outcomes(fake_redis, datetime.now(UTC), 3600) == []


async def test_claim_outcome_takes_once_then_gone(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="failed",
        result={"error": "boom"},
        candidates=cands,
        retention_ttl=3600,
    )
    first = await store.claim_outcome(fake_redis, "cid")
    assert first is not None
    assert first.status == "failed"
    assert await store.claim_outcome(fake_redis, "cid") is None
    # Taken -> dropped from the subject index and the caller open set.
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()
    assert await fake_redis.zcard(store.open_caller_key) == 0


async def test_delete_outcome_clears_index_and_is_a_noop_when_gone(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
    )
    await store.delete_outcome(fake_redis, "cid")
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()
    await store.delete_outcome(fake_redis, "cid")  # gone -> no-op


async def test_outcome_counts_under_caller_cap_but_is_never_refused(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    # Cap the caller index at 1 with a live caller ask.
    caller = _park(store, "c1", candidates=_candidates(person="pC"))
    assert await store.reserve_open_slot(fake_redis, caller, 1, to="caller") is True
    # A finished run's outcome is written regardless — the cap never refuses it.
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
    )
    assert await store.claim_outcome(fake_redis, "cid") is not None
    # A FRESH caller ask at the full cap is still refused loudly.
    assert await store.reserve_open_slot(fake_redis, _park(store, "c2", gid="g5"), 1, to="caller") is False


# -- the caller-ask gate on the user-facing reads ----------------------------


async def test_pending_and_list_pending_hide_caller_asks(fake_redis):
    store = InteractionStore("t:")
    await store.add(fake_redis, _park(store, "u1", candidates=_candidates(person="pU")), idle_ttl=86400)
    await store.add(
        fake_redis, _park(store, "c1", gid="g2", candidates=_candidates(person="pC")), idle_ttl=86400, to="caller"
    )
    pending_ids = [req.interaction_id for req in await store.pending(fake_redis)]
    assert pending_ids == ["u1"]
    listed = await store.list_pending(fake_redis, now=datetime.now(UTC))
    assert [item["interaction_id"] for item in listed] == ["u1"]


# -- the continuation-due record carries delivery + run_delivery_id ----------


async def test_continuation_due_carries_delivery_and_run_identity(fake_redis):
    store = InteractionStore("t:")
    delivery = {"tool": "conversation_deliver", "context": {"chat": "c1"}}
    await store.add(
        fake_redis,
        _park(store, "i1", candidates=_candidates(person="pA")),
        idle_ttl=86400,
        to="caller",
        delivery=delivery,
        run_delivery_id="rid-1",
    )
    await store.record_answer(
        fake_redis,
        _answer("i1"),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    due = await store.claim_continuation_retry(fake_redis, "i1", datetime.now(UTC), 1000, 60000)
    assert isinstance(due, ContinuationDue)
    assert due.delivery == delivery
    assert due.run_delivery_id == "rid-1"


# -- person merge re-keys the index and the stored subject -------------------


async def test_rekey_subject_moves_index_scope_and_stored_context(fake_redis):
    store = InteractionStore("t:")
    # Two parks addressed to person "pA" under two different scopes.
    await store.add(
        fake_redis,
        _park(store, "i1", candidates=_candidates(target_name="a", person="pA")),
        idle_ttl=86400,
        to="caller",
    )
    await store.add(
        fake_redis,
        _park(store, "i2", gid="g2", candidates=_candidates(target_name="b", person="pA")),
        idle_ttl=86400,
        to="caller",
    )
    moved = await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")
    assert sorted(moved) == ["i1", "i2"]
    # Index moved to the new key, both scopes; the old key drained.
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pB")) == {"i1"}
    assert await fake_redis.smembers(store.subject_parks_key("agent", "b", "person", "pB")) == {"i2"}
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()
    assert await fake_redis.smembers(store.subject_scopes_key("person", "pB")) == {"agent:a", "agent:b"}
    # The stored subject (denormalized descriptor + request context) points at the new key.
    assert await fake_redis.hget(store.state_key("i1"), "subjects") is not None
    state = await store.get_state(fake_redis, "i1")
    assert state is not None
    context = state.request.continuation_state_context
    assert context is not None
    assert context.candidates.by_kind["person"] == "pB"


async def test_rekey_subject_rewrites_the_continuation_due_state_context(fake_redis):
    store = InteractionStore("t:")
    ctx = _context(_candidates(person="pA"))
    # A running entry reached by the merge walk: its subject membership plus a live continuation-due
    # record whose ``state_context`` copy — the one the detached redelivery leg reads to subject-track
    # the resumed run's outcome — still names the absorbed person.
    await fake_redis.sadd(store.subject_parks_key("agent", "a", "person", "pA"), "cd1")
    await fake_redis.sadd(store.subject_scopes_key("person", "pA"), "agent:a")
    await fake_redis.hset(
        store.continuation_due_key("cd1"),
        mapping={
            "tool": "resume_tool",
            "identity": "svc-key",
            "fingerprint": "fp-1",
            "answer": "null",
            "attempts": "0",
            "state_context": ctx.model_dump_json(),
        },
    )
    await fake_redis.zadd(store.continuation_due_index_key, {"cd1": 0})

    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")

    # Membership moved AND the due-record copy the redelivery leg reads lands on the survivor's key.
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pB")) == {"cd1"}
    due = await store.claim_continuation_retry(fake_redis, "cd1", datetime.now(UTC), 1000, 60000)
    assert isinstance(due, ContinuationDue)
    assert due.state_context is not None
    assert due.state_context.candidates.by_kind["person"] == "pB"


async def test_rekey_subject_rewrites_the_kill_due_subjects(fake_redis):
    store = InteractionStore("t:")
    descriptor = {"target_kind": "agent", "target_name": "a", "by_kind": {"person": "pA"}}
    # An answered-and-killed sibling still listed under the subject: its kill-due record's ``subjects``
    # copy — the one the crash-redelivery leg reads to subject-track the killed run's FAILED — still
    # names the absorbed person.
    await fake_redis.sadd(store.subject_parks_key("agent", "a", "person", "pA"), "kd1")
    await fake_redis.sadd(store.subject_scopes_key("person", "pA"), "agent:a")
    await fake_redis.hset(
        store.kill_due_key("kd1"),
        mapping={
            "reason": "erased",
            "attempts": "0",
            "deadline_ms": "9999999999999",
            "run_delivery_id": "rd-k",
            "subjects": json.dumps(descriptor),
        },
    )
    await fake_redis.zadd(store.kill_due_index_key, {"kd1": 0})

    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")

    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pB")) == {"kd1"}
    due = await store.claim_kill_retry(fake_redis, "kd1", datetime.now(UTC), 1000, 60000)
    assert isinstance(due, KillDue)
    assert due.subjects is not None
    assert due.subjects["by_kind"]["person"] == "pB"


async def test_rekey_subject_updates_a_waiting_outcome_membership(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add_outcome(
        fake_redis,
        completion_id="cid",
        interaction_id="i1",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
    )
    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pB")) == {"cid"}
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pA")) == set()


# -- kill-due key shapes (used by the whole-chain kill) -----------------------


def test_kill_due_key_shapes():
    store = InteractionStore("t:")
    assert store.kill_due_key("i1") == "t:kill:due:i1"
    assert store.kill_due_index_key == "t:kill:due"


# -- list_parked_for status resolution edge cases ----------------------------


async def test_list_parked_for_drops_a_resolved_entry_with_no_due(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    await store.add(fake_redis, _park(store, "i1", candidates=cands), idle_ttl=86400, to="caller")
    await store.record_answer(
        fake_redis,
        _answer("i1"),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    # The continuation completed and cleared its due record: the answered state has no
    # due and no outcome, so it drops out of the listing.
    await store.clear_continuation_due(fake_redis, "i1")
    assert await store.list_parked_for(fake_redis, cands) == []


async def test_list_parked_for_reports_running_when_state_expired(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    parks_key = store.subject_parks_key("agent", "a", "person", "pA")
    # A member whose state hash has expired but whose continuation-due record still
    # stands (state TTL < due TTL): it resolves to a minimal ``running`` entry.
    await fake_redis.sadd(parks_key, "i1")
    await fake_redis.hset(store.continuation_due_key("i1"), mapping={"tool": "resume_tool"})
    entries = await store.list_parked_for(fake_redis, cands)
    assert entries == [{"id": "i1", "status": "running"}]


async def test_list_parked_for_skips_a_dangling_member(fake_redis):
    store = InteractionStore("t:")
    cands = _candidates(person="pA")
    # A stale membership whose backing record is entirely gone is skipped.
    await fake_redis.sadd(store.subject_parks_key("agent", "a", "person", "pA"), "ghost")
    assert await store.list_parked_for(fake_redis, cands) == []


async def test_claim_outcome_raises_on_a_torn_status(fake_redis):
    store = InteractionStore("t:")
    await fake_redis.hset(
        store.outcome_key("cid"),
        mapping={"completion_id": "cid", "status": "weird", "result": "1", "subjects": json.dumps({"by_kind": {}})},
    )
    with pytest.raises(RuntimeError, match="invalid status"):
        await store.claim_outcome(fake_redis, "cid")


# -- rekey edge cases --------------------------------------------------------


async def test_rekey_subject_is_a_noop_when_keys_match(fake_redis):
    store = InteractionStore("t:")
    assert await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pA") == []


async def test_rekey_member_without_a_context_updates_only_the_descriptor(fake_redis):
    store = InteractionStore("t:")
    # A state-backed member indexed under the subject but whose stored request carries
    # no state context: the merge rewrites the denormalized descriptor and leaves the
    # (context-less) request untouched.
    descriptor = {"target_kind": "agent", "target_name": "a", "by_kind": {"person": "pA"}}
    await fake_redis.sadd(store.subject_parks_key("agent", "a", "person", "pA"), "m1")
    await fake_redis.sadd(store.subject_scopes_key("person", "pA"), "agent:a")
    await fake_redis.hset(
        store.state_key("m1"),
        mapping={
            "status": "pending",
            "group_id": "g1",
            "request": _park(store, "m1").model_dump_json(),
            "subjects": json.dumps(descriptor),
        },
    )
    await store.rekey_subject(fake_redis, kind="person", old_key="pA", new_key="pB")
    moved_descriptor = json.loads(await fake_redis.hget(store.state_key("m1"), "subjects"))
    assert moved_descriptor["by_kind"]["person"] == "pB"
    assert await fake_redis.smembers(store.subject_parks_key("agent", "a", "person", "pB")) == {"m1"}


# -- the call chain survives the store round-trip to the reaper redelivery ----


async def test_asked_by_survives_the_store_round_trip_to_the_due_record(fake_redis):
    store = InteractionStore("t:")
    req = _park(store, "i1", candidates=_candidates(person="pA")).model_copy(update={"asked_by": ["main", "sub"]})
    await store.add(fake_redis, req, idle_ttl=86400, to="caller")
    # Denormalized onto the state hash at park time.
    assert json.loads(await fake_redis.hget(store.state_key("i1"), "asked_by")) == ["main", "sub"]
    await store.record_answer(
        fake_redis,
        _answer("i1"),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    # ...carried into the durable continuation-due record and read back for the
    # reaper's detached redelivery (which never re-reads the request).
    due = await store.claim_continuation_retry(fake_redis, "i1", datetime.now(UTC), 1000, 60000)
    assert isinstance(due, ContinuationDue)
    assert due.asked_by == ["main", "sub"]
