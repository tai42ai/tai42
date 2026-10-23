"""A failure no retry can change, raised before the hook acts, holds the run as failed after one attempt."""

from __future__ import annotations

import pytest
from tai42_contract.errors import ERROR_KIND_ATTR, ErrorKind

from tai42_skeleton.hooks.managers import base_hooks_manager
from tai42_skeleton.operations.errors import ConflictError, NotFoundError, PermissionDeniedError

from .conftest import get_run, hook


def _cancelled() -> Exception:
    exc = RuntimeError("the door was told to stop")
    setattr(exc, ERROR_KIND_ATTR, ErrorKind.CANCELLED.value)
    return exc


@pytest.mark.parametrize(
    ("make", "kind"),
    [
        (lambda: ValueError("bad door input"), "bad_input"),
        (lambda: PermissionDeniedError("not allowed"), "unauthorized"),
        (lambda: NotFoundError("no such entry"), "not_found"),
        (lambda: ConflictError("already resolved"), "conflict"),
        (_cancelled, "cancelled"),
    ],
)
async def test_a_terminal_kind_fails_at_once(make, kind, backend, world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))

    async def _evaluate(contract, payload, parked):
        raise make()

    monkeypatch.setattr(base_hooks_manager, "evaluate_door_contract", _evaluate)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.attempts == 1
    assert run.acted == ""
    assert run.last_error_kind == kind
    assert run.failed_at == clock.t
    failed = await manager.list_hook_runs("failed", 10, None)
    assert [item.id for item in failed.items] == [run_id]
    assert world.tools.calls == []
