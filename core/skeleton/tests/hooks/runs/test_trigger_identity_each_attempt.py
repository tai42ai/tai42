"""A trigger link's execution key is re-checked on every attempt; the tool runs as the hook's own key."""

from __future__ import annotations

import pytest

from tai42_skeleton.authz import execution
from tai42_skeleton.hooks.runs.models import DispatchKey
from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import all_runs, get_run, held_run, hook

_LINK = DispatchKey("k-link", "fp-link")


@pytest.fixture
def revoked(monkeypatch) -> set[str]:
    """The execution keys whose identity build is refused, as a revoked key's is."""
    keys: set[str] = set()
    real = execution.build_execution_identity

    async def _build(execution_key, *, bound_fingerprint):
        if execution_key in keys:
            raise execution.ExecutionKeyAuthorityError(
                f"access denied: execution key {execution_key!r} has no policy", principal=execution_key, defect="x"
            )
        return await real(execution_key, bound_fingerprint=bound_fingerprint)

    monkeypatch.setattr(execution, "build_execution_identity", _build)
    return keys


async def test_a_revoked_link_key_fails_the_attempt_unauthorized(
    backend, world, hook_runs, clock, notices, revoked
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    hook_runs.off_serving_loop()
    [run_id] = (await manager.on_event("t", {"n": 1}, dispatch_key=_LINK)).run_ids
    assert (await held_run(manager, run_id)).dispatch_key == _LINK

    revoked.add("k-link")
    hook_runs.back_on_serving_loop()
    await sweep_tick()
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.last_error_kind, run.acted, run.attempts) == ("failed", "unauthorized", "", 1)
    assert world.tools.calls == []
    assert [n for n in notices if run_id in n]


async def test_a_link_key_revoked_between_attempts_fails_the_next(backend, world, hook_runs, clock, revoked) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.interactions.parked_faults = [ConnectionError("store down")]
    [run_id] = (await manager.on_event("t", {"n": 1}, dispatch_key=_LINK)).run_ids
    await hook_runs.drain()
    assert (await held_run(manager, run_id)).status == "pending"

    revoked.add("k-link")
    clock.advance(manager.settings.run_retry_base_seconds)
    await sweep_tick()
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.last_error_kind, run.attempts) == ("failed", "unauthorized", 2)
    assert world.tools.calls == []


async def test_a_valid_link_key_runs_the_tool_as_the_hook_key(backend, world, hook_runs, clock, revoked) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))

    await manager.on_event("t", {"n": 1}, dispatch_key=_LINK)
    await hook_runs.drain()

    assert await all_runs(manager) == []
    [call] = world.tools.calls
    assert call.user_id == "k-hook"
