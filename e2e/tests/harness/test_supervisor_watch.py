"""Harness self-tests for which processes the respawn-on-exit supervisor watches.

A supervised stack brings back every process a recycle can make exit: each serve worker, the
backend worker and each queue-label worker (``backend-queue-<label>``), every one a member of
the bus census a roll targets. The backend variant's extra processes and the metrics server are
no recycle target and stay unwatched."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tai42_e2e.stack import TaiStack
from tai42_e2e.supervisor import RespawnSupervisor

pytestmark = pytest.mark.needs("no-stack")


def _stack(names: list[str], *, queue_workers: tuple[str, ...]) -> TaiStack:
    """A bare supervised stack whose processes are ``names`` (no live process behind any)."""
    stack = object.__new__(TaiStack)
    stack.procs = dict.fromkeys(names)  # type: ignore[attr-defined]
    stack.config = SimpleNamespace(  # type: ignore[attr-defined]
        supervised=True, run_backend=True, queue_workers=queue_workers, green_pool_workers=False, name="watch"
    )
    stack.supervisor = RespawnSupervisor(stack)
    return stack


def test_the_supervisor_watches_every_bus_member_process_a_recycle_can_end() -> None:
    names = ["serve", "backend", "backend-queue-b", "backend-beat", "metrics"]
    stack = _stack(names, queue_workers=("b",))
    stack.supervisor.start()
    try:
        with stack.supervisor.lock:
            watched = {name for name in names if stack.supervisor.watches(name)}
    finally:
        stack.supervisor.stop()

    assert watched == {"serve", "backend", "backend-queue-b"}
