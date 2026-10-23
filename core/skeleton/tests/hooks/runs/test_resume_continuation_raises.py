"""A resumed continuation that raises leaves the redelivery to the interactions reaper, never to a hook retry."""

from __future__ import annotations

import pytest
from tai42_contract.template import TemplatedText

from tai42_skeleton.hooks.runs.sweep import sweep_tick
from tai42_skeleton.operations.errors import ConflictError

from .conftest import all_runs, flaky, get_run, held_run, operator, real_hook


@pytest.fixture
def visits(monkeypatch) -> list:
    from tai42_skeleton.interactions import visit as visit_module

    return flaky(monkeypatch, visit_module, "visit", [])


async def test_a_raising_continuation_fails_the_run_and_keeps_the_due_record(
    backend, real_world, hook_runs, clock, monkeypatch, visits
) -> None:
    from tai42_skeleton.interactions import continuation as continuation_module

    manager = hook_runs.use(backend.manager())
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='{id: "i1", payload: {kind: "boom"}}')))
    await real_world.park("i1")
    drives = flaky(monkeypatch, continuation_module, "_run_continuation", [])

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    assert len(drives) == 1
    assert await real_world.due_present("i1")
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.acted_ids, run.attempts) == ("failed", "resume", ("i1",), 1)
    assert run.last_error is not None
    assert "resume drive blew up" in run.last_error

    clock.advance(manager.settings.run_retry_cap_seconds * 10)
    await sweep_tick()
    await hook_runs.drain()
    assert (await held_run(manager, run_id)).attempts == 1

    ops = operator(monkeypatch, manager)
    with pytest.raises(ConflictError, match="interactions reaper"):
        await ops.retry_hook_run(run_id)
    await ops.discard_hook_run(run_id)
    assert await all_runs(manager) == []
    assert len(visits) == 1
    assert len(drives) == 1


async def test_a_sibling_fault_after_the_first_commit_records_the_first(
    backend, real_world, hook_runs, clock, monkeypatch, visits
) -> None:
    from tai42_skeleton.interactions.store import InteractionStore

    manager = hook_runs.use(backend.manager())
    resume = '[{id: "i1", payload: {value: 1}}, {id: "i2", payload: {value: 2}}]'
    await manager.register(real_hook("a", resume_expr=TemplatedText(content=resume)))
    await real_world.park("i1")
    await real_world.park("i2")
    committed: list = flaky(monkeypatch, InteractionStore, "record_answer", [])
    real_get_state = InteractionStore.get_state

    async def _get_state(self, redis, iid):
        if committed and iid == "i2":
            raise ConnectionError("store down on the second sibling")
        return await real_get_state(self, redis, iid)

    monkeypatch.setattr(InteractionStore, "get_state", _get_state)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.acted_ids) == ("failed", "resume", ("i1",))
    assert run.last_error is not None
    assert "store down on the second sibling" in run.last_error
    assert len(committed) == 1
    assert await real_world.status("i2") == "pending"
    assert len(visits) == 1
