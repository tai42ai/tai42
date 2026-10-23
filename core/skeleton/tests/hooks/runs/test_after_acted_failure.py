"""Once the hook acted, a failure is held as failed at once; an operator retry replays the start alone."""

from __future__ import annotations

import logging

import pytest
from tai42_contract.interactions import ParkedEntryGoneError
from tai42_contract.states import StateAttach, StateBinding
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_skeleton.hooks.managers import base_hooks_manager
from tai42_skeleton.hooks.runs.loud import HOOK_RUN_FAILED_EVENT_TOPIC
from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import all_runs, fail_times, get_run, hook, operator, real_hook

_BINDING = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".who"))])


async def test_the_start_marker_is_stored_before_the_tool_runs(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    # Stored directly: the binding's template check needs a registered state the fake app has none of.
    await manager._store_hook(
        real_hook(
            "a",
            start_expr=TemplatedText(content="{n: .n}"),
            extras_expr=TemplatedText(content='{"x": 1}'),
            state_binding=_BINDING,
        )
    )
    seen: dict = {}

    async def _look(call):
        [run] = await all_runs(manager)
        seen["run"] = run
        return {"ok": True}

    world.tools.behave["probe"] = _look

    [run_id] = (await manager.on_event("t", {"n": 1, "who": "pA"})).run_ids
    await hook_runs.drain()

    run = seen["run"]
    assert run.id == run_id
    assert run.acted == "start"
    assert run.start is not None
    assert run.start.tool == "probe"
    assert run.start.input == {"n": 1}
    assert run.start.extras == {"x": 1}
    assert TOOL_EXTRA_IDEMPOTENCY_KEY not in run.start.extras
    assert run.start.state_context is not None
    assert run.start.state_context.candidates.by_kind == {"person": "pA"}
    assert run.start.state_binding == _BINDING


async def test_a_cancel_is_marked_before_the_park_is_killed(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a", cancel_expr=TemplatedText(content='["i9"]')))
    during: list = []

    async def _on_cancel(ids):
        [run] = await all_runs(manager)
        during.append((run.acted, run.acted_ids))

    world.interactions.on_cancel = _on_cancel

    await manager.on_event("t", {"n": 1})
    await hook_runs.drain()

    assert during == [("cancel", ("i9",))]
    assert world.interactions.cancelled == ["i9"]
    assert len(world.tools.calls) == 1


async def test_a_started_tool_that_fails_is_held_and_replayed_by_retry(
    backend, world, hook_runs, clock, notices, caplog, monkeypatch
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a", start_expr=TemplatedText(content="{n: .n}")))
    await manager.register(hook("watch", topic=HOOK_RUN_FAILED_EVENT_TOPIC, tool="watcher"))
    world.tools.behave["probe"] = fail_times(1, lambda: ConnectionError("upstream down"))
    evaluations: list = []
    real_evaluate = base_hooks_manager.evaluate_door_contract

    async def _evaluate(contract, payload, parked):
        evaluations.append(contract.name)
        return await real_evaluate(contract, payload, parked)

    monkeypatch.setattr(base_hooks_manager, "evaluate_door_contract", _evaluate)
    caplog.set_level(logging.ERROR, logger="tai42_skeleton.hooks.runs.loud")

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.acted, run.last_error_kind) == ("failed", 1, "start", "unavailable")
    assert [r for r in caplog.records if run_id in r.getMessage()]
    assert [n for n in notices if run_id in n]
    assert len(world.tools.of("watcher")) == 1

    clock.advance(manager.settings.run_retry_cap_seconds * 2)
    await sweep_tick()
    await hook_runs.drain()
    assert len(world.tools.of("probe")) == 1

    ops = operator(monkeypatch, manager)
    view = await ops.retry_hook_run(run_id)
    await hook_runs.drain()

    assert view["id"] == run_id
    assert view["acted"] == "start"
    assert evaluations == ["a", "watch"]
    first, second = world.tools.of("probe")
    assert second.input == first.input == {"n": 1}
    assert second.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == first.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id
    assert await get_run(manager, run_id) is None


async def test_a_visit_pre_check_refusal_is_before_acted(backend, real_world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='{id: "nowhere", payload: 1}')))

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.acted, run.last_error_kind) == ("failed", 1, "", "conflict")
    assert ParkedEntryGoneError.__name__ in (run.last_error or "")

    ops = operator(monkeypatch, manager)
    view = await ops.retry_hook_run(run_id)
    await hook_runs.drain()
    assert view["status"] == "running"
    again = await get_run(manager, run_id)
    assert again is not None
    assert (again.status, again.attempts, again.acted) == ("failed", 1, "")


async def test_a_failed_committed_marker_still_records_the_resume(
    backend, real_world, hook_runs, clock, monkeypatch
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='{id: "i1", payload: {kind: "boom"}}')))
    await real_world.park("i1")
    original = manager._mark_acted

    async def _mark(run_id, claimed_by, act, start_spec):
        if act.phase == "committed":
            raise ConnectionError("run store down")
        return await original(run_id, claimed_by, act, start_spec)

    monkeypatch.setattr(manager, "_mark_acted", _mark)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.acted_ids, run.claim_intent) == ("failed", "resume", ("i1",), None)
    assert await real_world.status("i1") == "answered"
    with pytest.raises(Exception, match="resumed"):
        await operator(monkeypatch, manager).retry_hook_run(run_id)


@pytest.mark.parametrize(("fail_times", "left"), [(99, 1), (2, 0)])
async def test_a_tool_retry_policy_retries_inside_the_one_attempt(
    fail_times, left, backend, real_world, hook_runs, clock
) -> None:
    from tests.hooks.runs import _retry_tool

    _retry_tool.calls.clear()
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a", tool="retried_probe", start_expr=TemplatedText(content="{fail_times: .f}")))

    [run_id] = (await manager.on_event("t", {"f": fail_times})).run_ids
    await hook_runs.drain()

    assert len(_retry_tool.calls) == 3
    runs = await all_runs(manager)
    assert len(runs) == left
    if left:
        [run] = runs
        assert (run.id, run.status, run.attempts, run.acted) == (run_id, "failed", 1, "start")
