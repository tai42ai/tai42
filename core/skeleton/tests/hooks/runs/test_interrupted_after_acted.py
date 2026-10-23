"""A job reclaimed after its holder died once the hook acted: a flagged start is replayed alone, all else fails."""

from __future__ import annotations

import pytest
from tai42_contract.interactions import VisitAct
from tai42_contract.states import StateAttach, StateBinding, StateContext
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_skeleton.hooks.managers import base_hooks_manager
from tai42_skeleton.hooks.runs import attempt as attempt_module
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.models import StartSpec
from tai42_skeleton.hooks.runs.sweep import sweep_tick
from tai42_skeleton.operations.errors import ConflictError

from .conftest import as_process, get_run, hook, operator, park_candidates

DEAD = "dead-host:1:aaaa"
_BINDING = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".who"))])
_CONTEXT = StateContext(door="hook", candidates=park_candidates(), actor="k-hook", turn_id=None)
_START = StartSpec(tool="probe", input={"n": 1}, extras={"x": 1}, state_context=_CONTEXT, state_binding=_BINDING)


async def acted_then_died(manager, monkeypatch, acts: list[tuple[VisitAct, StartSpec | None]]) -> str:
    """Accept as a process that records ``acts`` on its job and then dies; returns the run id."""
    spawn = runtime.spawn_attempt
    as_process(monkeypatch, DEAD)
    monkeypatch.setattr(runtime, "spawn_attempt", lambda job: None)
    [run_id] = (await manager.on_event("t", {"n": 1, "who": "pA"})).run_ids
    for act, start in acts:
        assert await manager._mark_acted(run_id, DEAD, act, start)
    as_process(monkeypatch, "live-host:2:bbbb")
    monkeypatch.setattr(runtime, "spawn_attempt", spawn)
    return run_id


async def reclaim(manager, hook_runs, clock) -> None:
    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()


@pytest.fixture
def spies(monkeypatch) -> dict[str, list]:
    seen: dict[str, list] = {"evaluate": [], "bind": []}
    real_evaluate = base_hooks_manager.evaluate_door_contract
    real_bind = attempt_module.bind_execution_identity

    async def _evaluate(contract, payload, parked):
        seen["evaluate"].append(contract.name)
        return await real_evaluate(contract, payload, parked)

    def _bind(key, *, bound_fingerprint=""):
        seen["bind"].append(key)
        return real_bind(key, bound_fingerprint=bound_fingerprint)

    monkeypatch.setattr(base_hooks_manager, "evaluate_door_contract", _evaluate)
    monkeypatch.setattr(attempt_module, "bind_execution_identity", _bind)
    return seen


_CANCEL_THEN_START = [
    (VisitAct("cancel", ("i9",), "before"), None),
    (VisitAct("start", (), "before"), _START),
]


async def test_a_flagged_start_is_replayed_alone(backend, world, hook_runs, clock, monkeypatch, spies) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a", cancel_expr=TemplatedText(content='["i9"]')))
    world.tools.flagged = {"probe"}
    run_id = await acted_then_died(manager, monkeypatch, _CANCEL_THEN_START)

    await reclaim(manager, hook_runs, clock)

    assert await get_run(manager, run_id) is None
    [visit] = world.interactions.visit_calls
    assert (visit.cancel, visit.resume, visit.extras, visit.state_binding) == ([], [], {"x": 1}, _BINDING)
    assert world.interactions.parked_reads == []
    assert spies["evaluate"] == []
    assert spies["bind"] == ["k-hook"]
    [call] = world.tools.calls
    assert call.input == {"n": 1}
    assert call.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id
    assert call.extras["x"] == 1
    assert call.state_context == _CONTEXT
    assert call.invocation is not None
    assert call.invocation.state_binding == _BINDING


async def test_an_unflagged_start_fails_and_a_retry_replays_it(
    backend, world, hook_runs, clock, monkeypatch, notices, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    run_id = await acted_then_died(manager, monkeypatch, [(VisitAct("start", (), "before"), _START)])

    await reclaim(manager, hook_runs, clock)

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.last_error) == ("failed", "start", "interrupted after the hook acted (start)")
    assert world.tools.calls == []
    assert [n for n in notices if run_id in n]

    await operator(monkeypatch, manager).retry_hook_run(run_id)
    await hook_runs.drain()

    [call] = world.tools.calls
    assert call.input == {"n": 1}
    assert call.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id
    assert spies["evaluate"] == []
    assert await get_run(manager, run_id) is None


@pytest.mark.parametrize(("action", "phrase"), [("resume", "interactions reaper"), ("take", "nothing redelivers")])
async def test_a_committed_claim_is_never_rerun(action, phrase, backend, world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.tools.flagged = {"probe"}
    run_id = await acted_then_died(
        manager,
        monkeypatch,
        [(VisitAct(action, ("i1",), "before"), None), (VisitAct(action, ("i1",), "committed"), None)],
    )

    await reclaim(manager, hook_runs, clock)

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.acted_ids) == ("failed", action, ("i1",))
    assert run.last_error == f"interrupted after the hook acted ({action})"
    assert world.tools.calls == []
    assert world.interactions.visit_calls == []
    with pytest.raises(ConflictError, match=phrase):
        await operator(monkeypatch, manager).retry_hook_run(run_id)


async def test_a_cancel_fails_and_a_retry_runs_the_hook_again(
    backend, world, hook_runs, clock, monkeypatch, spies
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    run_id = await acted_then_died(manager, monkeypatch, [(VisitAct("cancel", ("i9",), "before"), None)])

    await reclaim(manager, hook_runs, clock)

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.acted_ids) == ("failed", "cancel", ("i9",))

    await operator(monkeypatch, manager).retry_hook_run(run_id)
    await hook_runs.drain()

    assert spies["evaluate"] == ["a"]
    assert len(world.interactions.parked_reads) == 1
    assert len(world.tools.calls) == 1
    assert await get_run(manager, run_id) is None
