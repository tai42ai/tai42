"""An unconfirmed resume or take claim is settled from the interactions store before anything else.

The claim is re-run automatically only while its entries are still open. An entry no longer open
cannot be told apart from another door's resolution, so the job is held as failed with the intent
kept, and never recorded as committed from that read.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from tai42_contract.interactions import VisitAct
from tai42_contract.template import TemplatedText

from tai42_skeleton.hooks.managers import base_hooks_manager
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.models import ClaimIntent
from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep, sweep_tick
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.store import InteractionStore

from .conftest import all_runs, as_process, get_run, held_run, metric, operator, real_hook

_RESUME = '{id: "i1", payload: {value: 1}}'
_BOOM = '{id: "i1", payload: {kind: "boom"}}'
DEAD = "dead-host:1:aaaa"
LIVE = "live-host:2:bbbb"


class _HolderDied(BaseException):
    """Ends an attempt task with no verdict, as the death of the process holding it would."""


@pytest.fixture
def spies(monkeypatch) -> dict[str, list]:
    """Count the visits, the door-contract evaluations, the settles and the ``$parked`` reads, in order."""
    seen: dict[str, list] = {"visit": [], "evaluate": [], "settle": [], "order": []}
    real_visit = visit_module.visit
    real_settle = visit_module.settle_visit_claim
    real_parked = visit_module.list_parked_for
    real_evaluate = base_hooks_manager.evaluate_door_contract

    async def _visit(**kwargs):
        seen["visit"].append(kwargs["resume"])
        return await real_visit(**kwargs)

    async def _settle(act):
        seen["settle"].append(act)
        seen["order"].append("settle")
        return await real_settle(act)

    async def _parked(context):
        seen["order"].append("list_parked_for")
        return await real_parked(context)

    async def _evaluate(contract, payload, parked):
        seen["evaluate"].append(contract.name)
        return await real_evaluate(contract, payload, parked)

    monkeypatch.setattr(visit_module, "visit", _visit)
    monkeypatch.setattr(visit_module, "settle_visit_claim", _settle)
    monkeypatch.setattr(visit_module, "list_parked_for", _parked)
    monkeypatch.setattr(base_hooks_manager, "evaluate_door_contract", _evaluate)
    return seen


def _stop_on(monkeypatch, manager, phase: str, action: str, make) -> None:
    """Make ``manager``'s first ``phase`` marker of ``action`` raise ``make()`` instead of writing."""
    original = manager._mark_acted
    fired = {"n": 0}

    async def _mark(run_id, claimed_by, act, start_spec):
        if act.phase == phase and act.action == action and fired["n"] == 0:
            fired["n"] += 1
            raise make()
        return await original(run_id, claimed_by, act, start_spec)

    monkeypatch.setattr(manager, "_mark_acted", _mark)


async def _reclaim_as_peer(backend, manager, hook_runs, clock, monkeypatch):
    """Let the lease lapse, then reclaim from a second manager under another claimer id."""
    peer = hook_runs.use(backend.peer(manager))
    as_process(monkeypatch, LIVE)
    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()
    return peer


def _assert_untold(run, ids, *, claim_error: str | None = None) -> None:
    assert run is not None
    assert (run.status, run.acted, run.last_error_kind) == ("failed", "", "conflict")
    assert run.claim_intent is not None
    assert run.claim_intent.ids == tuple(ids)
    assert f"These are no longer open: {list(ids)}" in (run.last_error or "")
    assert "cannot tell whether this run's claim resolved them" in (run.last_error or "")
    if claim_error is not None:
        assert f"The claim call failed with: {claim_error}" in (run.last_error or "")


# -- a claim committed, then the holder died before confirming it ------------------------


async def test_a_resume_committed_then_the_holder_died_cannot_be_told(
    backend, real_world, hook_runs, clock, monkeypatch, spies, notices
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    as_process(monkeypatch, DEAD)
    _stop_on(monkeypatch, manager, "committed", "resume", _HolderDied)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    held = await held_run(manager, run_id)
    assert (held.status, held.acted, held.claim_intent) == ("running", "", ClaimIntent("resume", ("i1",)))

    peer = await _reclaim_as_peer(backend, manager, hook_runs, clock, monkeypatch)

    _assert_untold(await get_run(peer, run_id), ["i1"])
    assert len(spies["visit"]) == 1
    assert spies["evaluate"] == ["a"]
    assert await real_world.status("i1") == "answered"
    assert await real_world.due_present("i1")
    assert [n for n in notices if run_id in n]


async def test_a_take_committed_then_the_holder_died_cannot_be_told(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='"cid"'), start_expr=None))
    await real_world.outcome("cid", "finished", {"v": 1})
    as_process(monkeypatch, DEAD)
    _stop_on(monkeypatch, manager, "committed", "take", _HolderDied)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    peer = await _reclaim_as_peer(backend, manager, hook_runs, clock, monkeypatch)

    _assert_untold(await get_run(peer, run_id), ["cid"])
    assert not await real_world.outcome_waiting("cid")
    assert len(spies["visit"]) == 1
    assert spies["evaluate"] == ["a"]


async def test_a_lost_lease_on_the_confirm_changes_nothing_and_the_next_holder_settles(
    backend, real_world, hook_runs, clock, monkeypatch, spies, caplog
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    as_process(monkeypatch, DEAD)
    original = manager._mark_acted
    fenced: list[bool] = []

    async def _mark(run_id, claimed_by, act, start_spec):
        if act.phase == "committed" and not fenced:
            fenced.append(True)
            return False
        return await original(run_id, claimed_by, act, start_spec)

    monkeypatch.setattr(manager, "_mark_acted", _mark)
    caplog.set_level(logging.WARNING)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    held = await held_run(manager, run_id)
    assert (held.status, held.acted, held.claim_intent) == ("running", "", ClaimIntent("resume", ("i1",)))
    assert [r for r in caplog.records if "lost its lease" in r.getMessage() and run_id in r.getMessage()]

    peer = await _reclaim_as_peer(backend, manager, hook_runs, clock, monkeypatch)
    _assert_untold(await get_run(peer, run_id), ["i1"])
    assert len(spies["visit"]) == 1


# -- the verdict write fails -------------------------------------------------------------


async def test_a_failed_verdict_after_a_confirmed_resume_is_failed_by_the_reclaim(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_BOOM), start_expr=None))
    await real_world.park("i1")
    as_process(monkeypatch, DEAD)
    fail_run = manager._fail_run
    refused: list[str] = []

    async def _fail_once(*args):
        if not refused:
            refused.append(args[0])
            raise ConnectionError("run store down")
        return await fail_run(*args)

    monkeypatch.setattr(manager, "_fail_run", _fail_once)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    held = await held_run(manager, run_id)
    assert (held.status, held.acted, held.claim_intent) == ("running", "resume", None)

    peer = await _reclaim_as_peer(backend, manager, hook_runs, clock, monkeypatch)
    run = await held_run(peer, run_id)
    assert (run.status, run.acted, run.last_error) == (
        "failed",
        "resume",
        "interrupted after the hook acted (resume)",
    )
    assert refused == [run_id]
    assert len(spies["visit"]) == 1


async def test_a_failed_confirm_and_a_failed_verdict_are_settled_by_the_reclaim(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    as_process(monkeypatch, DEAD)
    _stop_on(monkeypatch, manager, "committed", "resume", lambda: ConnectionError("run store down"))
    fail_run = manager._fail_run
    refused: list[str] = []

    async def _fail_once(*args):
        if not refused:
            refused.append(args[0])
            raise ConnectionError("run store down")
        return await fail_run(*args)

    monkeypatch.setattr(manager, "_fail_run", _fail_once)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    held = await held_run(manager, run_id)
    assert (held.status, held.acted, held.claim_intent) == ("running", "", ClaimIntent("resume", ("i1",)))

    peer = await _reclaim_as_peer(backend, manager, hook_runs, clock, monkeypatch)
    _assert_untold(await get_run(peer, run_id), ["i1"])
    assert refused == [run_id]
    assert len(spies["visit"]) == 1


# -- the settle itself -------------------------------------------------------------------


async def _intent_left_by_a_dead_holder(manager, monkeypatch, intent: VisitAct) -> str:
    """Accept as a process that writes the ``before`` claim intent and dies; returns the run id."""
    spawn = runtime.spawn_attempt
    as_process(monkeypatch, DEAD)
    monkeypatch.setattr(runtime, "spawn_attempt", lambda job: None)
    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    assert await manager._mark_acted(run_id, DEAD, intent, None)
    monkeypatch.setattr(runtime, "spawn_attempt", spawn)
    return run_id


async def test_a_settle_fault_reschedules_with_the_intent_kept_and_settles_first_next_time(
    backend, real_world, hook_runs, clock, monkeypatch, spies, caplog
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    run_id = await _intent_left_by_a_dead_holder(manager, monkeypatch, VisitAct("resume", ("i1",), "before"))
    settle = visit_module.settle_visit_claim
    faults = [ConnectionError("interactions store down")]

    async def _flaky_settle(act):
        if faults:
            spies["order"].append("settle")
            raise faults.pop(0)
        return await settle(act)

    monkeypatch.setattr(visit_module, "settle_visit_claim", _flaky_settle)
    caplog.set_level(logging.ERROR)

    as_process(monkeypatch, LIVE)
    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()

    run = await held_run(manager, run_id)
    assert (run.status, run.acted, run.claim_intent, run.last_error_kind) == (
        "pending",
        "",
        ClaimIntent("resume", ("i1",)),
        "unavailable",
    )
    assert [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    assert spies["order"] == ["settle"]

    clock.advance(run.next_attempt_at - clock.t)
    await sweep_tick()
    await hook_runs.drain()

    assert spies["order"] == ["settle", "settle", "list_parked_for"]
    assert await all_runs(manager) == []
    assert await real_world.status("i1") == "answered"


async def test_a_lost_race_clears_the_intent_with_no_settle_and_fails_as_conflict(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")

    async def _lost(self, r, response, *args, **kwargs):
        return False

    record_answer = InteractionStore.record_answer
    monkeypatch.setattr(InteractionStore, "record_answer", _lost)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    run = await held_run(manager, run_id)
    assert (run.status, run.acted, run.claim_intent, run.last_error_kind) == ("failed", "", None, "conflict")
    assert "resolved by another caller" in (run.last_error or "")
    assert spies["settle"] == []
    monkeypatch.setattr(InteractionStore, "record_answer", record_answer)
    view = await operator(monkeypatch, manager).retry_hook_run(run_id)
    assert view["id"] == run_id
    await hook_runs.drain()
    assert await get_run(manager, run_id) is None
    assert await real_world.status("i1") == "answered"


async def test_a_claim_left_open_by_a_dead_holder_runs_again(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    run_id = await _intent_left_by_a_dead_holder(manager, monkeypatch, VisitAct("resume", ("i1",), "before"))
    open_before = metric("tai42_hook_runs_claim_settles_total", result="open")

    as_process(monkeypatch, LIVE)
    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()

    assert await get_run(manager, run_id) is None
    assert await real_world.status("i1") == "answered"
    assert len(spies["visit"]) == 1
    assert metric("tai42_hook_runs_claim_settles_total", result="open") - open_before == 1


# -- resolved elsewhere while the intent was unconfirmed ---------------------------------


def _answer(iid: str):
    from datetime import UTC, datetime

    from tai42_contract.interactions import InteractionResponse

    return InteractionResponse(interaction_id=iid, answer="yes", answered_by="u-2", answered_at=datetime.now(UTC))


@pytest.mark.parametrize("elsewhere", ["answered", "state_missing"])
async def test_a_resume_resolved_elsewhere_while_unconfirmed_cannot_be_told(
    elsewhere, backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    record_answer = InteractionStore.record_answer
    untold_before = metric("tai42_hook_runs_claim_settles_total", result="untold")

    async def _resolved_then_fault(self, r, response, *args, **kwargs):
        if elsewhere == "answered":
            # The answer door's own claim lands first.
            assert await record_answer(
                self,
                r,
                _answer(response.interaction_id),
                "g1",
                60,
                continuation_due_ttl=3600,
                continuation_first_attempt_at_ms=0,
            )
        else:
            await r.delete(self.state_key(response.interaction_id))
        raise ConnectionError("interactions store down")

    monkeypatch.setattr(InteractionStore, "record_answer", _resolved_then_fault)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    _assert_untold(run, ["i1"], claim_error="ConnectionError: interactions store down")
    assert metric("tai42_hook_runs_claim_settles_total", result="untold") - untold_before == 1
    clock.advance(manager.settings.run_retry_cap_seconds + manager.settings.run_lease_seconds)
    await sweep_tick()
    await hook_runs.drain()
    assert len(spies["visit"]) == 1
    assert (await held_run(manager, run_id)).acted == ""


async def test_a_take_removed_elsewhere_while_unconfirmed_cannot_be_told(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='"cid"'), start_expr=None))
    await real_world.outcome("cid", "finished", {"v": 1})

    async def _removed_then_fault(self, r, cid):
        await self.delete_outcome(r, cid)
        raise ConnectionError("interactions store down")

    monkeypatch.setattr(InteractionStore, "claim_outcome", _removed_then_fault)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    _assert_untold(await get_run(manager, run_id), ["cid"], claim_error="ConnectionError: interactions store down")
    assert len(spies["visit"]) == 1


async def test_a_claim_fault_whose_settle_also_fails_keeps_both_errors(
    backend, real_world, hook_runs, clock, monkeypatch, spies, caplog
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    record_answer = InteractionStore.record_answer

    async def _answered_then_reply_lost(self, r, response, *args, **kwargs):
        assert await record_answer(
            self,
            r,
            _answer(response.interaction_id),
            "g1",
            60,
            continuation_due_ttl=3600,
            continuation_first_attempt_at_ms=0,
        )
        raise ConnectionError("claim reply lost")

    settle = visit_module.settle_visit_claim
    faults = [ConnectionError("interactions store down")]

    async def _flaky_settle(act):
        if faults:
            raise faults.pop(0)
        return await settle(act)

    monkeypatch.setattr(InteractionStore, "record_answer", _answered_then_reply_lost)
    monkeypatch.setattr(visit_module, "settle_visit_claim", _flaky_settle)
    caplog.set_level(logging.ERROR)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    run = await held_run(manager, run_id)
    assert (run.status, run.acted, run.claim_intent, run.last_error_kind) == (
        "pending",
        "",
        ClaimIntent("resume", ("i1",)),
        "unavailable",
    )
    assert "ConnectionError: claim reply lost" in (run.last_error or "")
    assert "ConnectionError: interactions store down" in (run.last_error or "")
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    assert any("ConnectionError: claim reply lost" in message for message in errors), errors

    clock.advance(run.next_attempt_at - clock.t)
    await sweep_tick()
    await hook_runs.drain()

    run = await held_run(manager, run_id)
    _assert_untold(run, ["i1"])
    assert "ConnectionError: claim reply lost" in (run.last_error or "")
    assert len(spies["visit"]) == 1


# -- the operator retry of a claim that could not be told --------------------------------


async def _untold_run(manager, real_world, hook_runs, monkeypatch) -> str:
    await real_world.park("i1")
    record_answer = InteractionStore.record_answer

    async def _resolved_then_fault(self, r, response, *args, **kwargs):
        await record_answer(
            self,
            r,
            _answer(response.interaction_id),
            "g1",
            60,
            continuation_due_ttl=3600,
            continuation_first_attempt_at_ms=0,
        )
        raise ConnectionError("interactions store down")

    monkeypatch.setattr(InteractionStore, "record_answer", _resolved_then_fault)
    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()
    monkeypatch.setattr(InteractionStore, "record_answer", record_answer)
    return run_id


async def test_a_retry_of_an_untold_claim_is_a_fresh_run_the_pre_check_refuses(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    run_id = await _untold_run(manager, real_world, hook_runs, monkeypatch)
    settles = len(spies["settle"])

    view = await operator(monkeypatch, manager).retry_hook_run(run_id)
    assert view["claim_intent"] is None
    await hook_runs.drain()

    assert len(spies["settle"]) == settles
    run = await held_run(manager, run_id)
    assert (run.status, run.acted, run.claim_intent, run.last_error_kind) == ("failed", "", None, "conflict")
    assert "ParkedEntryGoneError" in (run.last_error or "")
    assert len(spies["visit"]) == 2


async def test_a_retry_of_an_untold_claim_whose_door_names_nothing_starts_the_tool(
    backend, real_world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    run_id = await _untold_run(manager, real_world, hook_runs, monkeypatch)
    await manager.register(
        real_hook("a", tool="retried_probe", tool_kwargs={"fail_times": 0}, start_expr=None, resume_expr=None)
    )
    # The manifest's load imports the tool module afresh, so it is read from there.
    from tests.hooks.runs import _retry_tool

    _retry_tool.calls.clear()
    ops = operator(monkeypatch, manager)

    await ops.retry_hook_run(run_id)
    await hook_runs.drain()

    assert await get_run(manager, run_id) is None
    assert _retry_tool.calls == [0]


async def test_an_untold_claim_can_be_discarded(backend, real_world, hook_runs, clock, monkeypatch, spies) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    run_id = await _untold_run(manager, real_world, hook_runs, monkeypatch)

    assert await operator(monkeypatch, manager).discard_hook_run(run_id) == {"discarded": run_id}
    assert await get_run(manager, run_id) is None


async def test_a_store_fault_freeing_a_cancelled_attempt_is_logged_and_the_cancel_stands(
    backend, real_world, hook_runs, clock, monkeypatch, caplog
) -> None:
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=0.05))
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=_RESUME), start_expr=None))
    await real_world.park("i1")
    claiming = asyncio.Event()

    async def _held(self, r, response, *args, **kwargs):
        claiming.set()
        await asyncio.Event().wait()

    async def _clear_fault(run_id, claimed_by):
        raise ConnectionError("run store down")

    monkeypatch.setattr(InteractionStore, "record_answer", _held)
    monkeypatch.setattr(manager, "_clear_intent", _clear_fault)
    caplog.set_level(logging.ERROR)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    [attempt] = [task for task in asyncio.all_tasks() if task.get_name() == f"tai-hook-run-{run_id}"]
    await claiming.wait()
    await stop_hook_runs_sweep()

    assert attempt.cancelled()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    assert any(r.exc_info and "run store down" in str(r.exc_info[1]) for r in errors), errors
    run = await held_run(manager, run_id)
    assert (run.status, run.acted, run.claim_intent) == ("running", "", ClaimIntent("resume", ("i1",)))
