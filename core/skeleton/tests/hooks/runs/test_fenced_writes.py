"""After a reclaim, every write of the former holder changes nothing and is logged."""

from __future__ import annotations

import logging

from tai42_contract.interactions import VisitAct

from tai42_skeleton.hooks.runs import runtime

from .conftest import all_runs, as_process, get_run, hook


async def test_the_former_holder_changes_nothing(backend, world, hook_runs, clock, monkeypatch, caplog) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    as_process(monkeypatch, "old-host:1:aaaa")
    monkeypatch.setattr(runtime, "spawn_attempt", lambda job: None)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids

    clock.advance(manager.settings.run_lease_seconds + 1)
    peer = backend.peer(manager)
    [claimed] = await peer._claim_due_runs(
        clock.t, 10, "new-host:2:bbbb", clock.t + manager.settings.run_lease_seconds, frozenset()
    )
    assert claimed.id == run_id
    before = await get_run(peer, run_id)
    caplog.set_level(logging.WARNING)

    old = "old-host:1:aaaa"
    assert await manager._mark_acted(run_id, old, VisitAct("cancel", ("i1",), "before"), None) is False
    assert await manager._finish_run(run_id, old) is False
    assert await manager._reschedule_run(run_id, old, clock.t + 5, "E: x", "unknown") is False
    assert await manager._fail_run(run_id, old, "E: x", "unknown", clock.t, "", (), None) is None
    assert await manager._release_run(run_id, old, clock.t) is False
    assert await manager._renew_lease(run_id, old, clock.t + 60) is False
    assert await manager._clear_intent(run_id, old) is False

    assert await get_run(peer, run_id) == before
    lost = [r for r in caplog.records if r.levelno == logging.WARNING and run_id in r.getMessage()]
    assert len(lost) == 7


async def test_a_lost_lease_at_the_start_notice_never_starts_the_tool(
    backend, world, hook_runs, clock, monkeypatch, caplog
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))

    async def _lost(run_id, claimed_by, act, start_spec):
        return False

    monkeypatch.setattr(manager, "_mark_acted", _lost)
    caplog.set_level(logging.WARNING, logger="tai42_skeleton.hooks.runs.attempt")

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()

    assert world.tools.calls == []
    assert [r for r in caplog.records if "lost its lease" in r.getMessage() and run_id in r.getMessage()]
    [run] = await all_runs(manager)
    assert (run.status, run.acted) == ("running", "")
