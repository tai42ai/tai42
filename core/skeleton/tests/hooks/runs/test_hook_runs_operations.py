"""The operator doors over kept hook runs: list, retry and discard."""

from __future__ import annotations

import pytest
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_skeleton.operations.errors import ConflictError, NotFoundError, ValidationRejectedError

from .conftest import all_runs, fail_times, held_run, hook, operator

_VIEW_FIELDS = {
    "id",
    "topic",
    "hook",
    "status",
    "attempts",
    "next_attempt_at",
    "acted",
    "acted_ids",
    "claim_intent",
    "last_error",
    "last_error_kind",
    "created_at",
    "failed_at",
}


async def _failed_after_start(manager, world, hook_runs) -> str:
    """Accept one event whose hook starts a tool that fails; the run is held failed with ``acted=start``."""
    world.tools.behave["probe"] = fail_times(1, lambda: RuntimeError("tool down"))
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()
    return run_id


async def test_list_filters_pages_and_never_shows_the_payload(backend, world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    failed_id = await _failed_after_start(manager, world, hook_runs)
    hook_runs.off_serving_loop()
    pending = []
    for i in range(3):
        # Each accept is due a second after the one before, so the soonest-due order is the accept order.
        clock.advance(1)
        pending.append((await manager.on_event("t", {"n": i})).run_ids[0])
    ops = operator(monkeypatch, manager)

    everything = await ops.list_hook_runs()
    assert (everything["outstanding"], everything["failed"]) == (3, 1)
    assert [item["id"] for item in everything["items"]] == [*pending, failed_id]
    for item in everything["items"]:
        assert set(item) == _VIEW_FIELDS
    failed = (await ops.list_hook_runs(status="failed"))["items"]
    assert [(item["id"], item["acted"], item["acted_ids"]) for item in failed] == [(failed_id, "start", [])]
    assert failed[0]["last_error"] is not None
    assert "tool down" in failed[0]["last_error"]

    first = await ops.list_hook_runs(status="outstanding", limit=2)
    assert [item["id"] for item in first["items"]] == pending[:2]
    rest = await ops.list_hook_runs(status="outstanding", limit=2, cursor=first["next_cursor"])
    assert [item["id"] for item in rest["items"]] == pending[2:]
    assert rest["next_cursor"] is None


@pytest.mark.parametrize("kwargs", [{"status": "pending"}, {"limit": 0}, {"limit": 201}, {"cursor": "not-a-cursor"}])
async def test_list_rejects_a_bad_query(kwargs, backend, world, hook_runs, monkeypatch) -> None:
    ops = operator(monkeypatch, hook_runs.use(backend.manager()))
    with pytest.raises(ValidationRejectedError):
        await ops.list_hook_runs(**kwargs)


async def test_retry_of_a_started_run_replays_the_start_with_its_id(backend, world, hook_runs, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    run_id = await _failed_after_start(manager, world, hook_runs)
    ops = operator(monkeypatch, manager)

    view = await ops.retry_hook_run(run_id)
    assert (view["id"], view["status"], view["acted"]) == (run_id, "running", "start")
    await hook_runs.drain()

    assert [call.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] for call in world.tools.calls] == [run_id, run_id]
    assert await all_runs(manager) == []


async def test_retry_of_a_run_that_had_not_acted_runs_the_hook_again(backend, world, hook_runs, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    hook_runs.off_serving_loop()
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await manager.unregister("a")
    hook_runs.back_on_serving_loop()
    from tai42_skeleton.hooks.runs.sweep import sweep_tick

    await sweep_tick()
    await hook_runs.drain()
    assert (await held_run(manager, run_id)).last_error_kind == "not_found"

    await manager.register(hook("a"))
    await operator(monkeypatch, manager).retry_hook_run(run_id)
    await hook_runs.drain()

    assert len(world.tools.calls) == 1
    assert len(world.interactions.parked_reads) == 1
    assert await all_runs(manager) == []


async def test_retry_and_discard_refuse_an_unknown_or_unfailed_run(backend, world, hook_runs, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    hook_runs.off_serving_loop()
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    ops = operator(monkeypatch, manager)

    with pytest.raises(ConflictError, match="is not failed"):
        await ops.retry_hook_run(run_id)
    with pytest.raises(ConflictError, match="is not failed"):
        await ops.discard_hook_run(run_id)
    with pytest.raises(NotFoundError, match="no hook run nope"):
        await ops.retry_hook_run("nope")
    with pytest.raises(NotFoundError, match="no hook run nope"):
        await ops.discard_hook_run("nope")
    assert (await held_run(manager, run_id)).status == "pending"


async def test_discard_deletes_a_failed_run_for_good(backend, world, hook_runs, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    run_id = await _failed_after_start(manager, world, hook_runs)
    ops = operator(monkeypatch, manager)

    assert await ops.discard_hook_run(run_id) == {"discarded": run_id}

    assert await all_runs(manager) == []
    with pytest.raises(NotFoundError):
        await ops.discard_hook_run(run_id)
    assert len(world.tools.calls) == 1
