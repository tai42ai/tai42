"""Harness self-tests for the backend-restart census wait
(:meth:`TaiStack._wait_backend_census`).

A restarted backend worker joins no port — its readiness keys on a fresh ``backend``-kind
census LIFE. A worker's slot name is STABLE across a restart, so a respawn reuses the same
name at an INCREMENTED generation; a SIGKILLed worker's corpse row lingers at the OLD
generation until its heartbeat TTL. The wait keys on the generation, so it ignores the
corpse and returns only once a fresh READY life has joined. These pins drive the wait with
a controllable census, no infra."""

from __future__ import annotations

import pytest

from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import BusWorker
from tai42_e2e.waiting import WaitTimeout


def _worker(name: str, generation: int, *, state: str = "ready", kind: str = "backend", pid: int = 1) -> BusWorker:
    return BusWorker(name=name, kind=kind, pid=pid, generation=generation, joined_at="t", beat_at="t", state=state)


def _stack(census: list[BusWorker]) -> TaiStack:
    """A bare stack whose census returns a fixed list and whose early-exit check
    is a no-op (no live processes to inspect)."""
    stack = object.__new__(TaiStack)
    stack._procs = {}  # type: ignore[attr-defined]
    stack.census = lambda: census  # type: ignore[method-assign]
    return stack


def test_wait_backend_census_ignores_the_pre_restart_corpse_at_the_old_generation() -> None:
    # Only the killed worker's corpse row (its slot name at the OLD generation) is present —
    # no fresh life has joined, so the restart wait must NOT pass on the corpse.
    stack = _stack([_worker("backend-1", 1)])
    with pytest.raises(WaitTimeout, match="fresh ready backend-kind life"):
        stack._wait_backend_census(0.2, baseline={"backend-1": 1})


def test_wait_backend_census_returns_on_the_next_generation_ready() -> None:
    # The restarted worker reused its slot name at generation+1 and went ready; the old life
    # is gone (superseded by the higher generation) beside the live serve fleet.
    stack = _stack([_worker("backend-1", 2), _worker("serve-1", 1, kind="serve", pid=3)])
    stack._wait_backend_census(1.0, baseline={"backend-1": 1})


def test_wait_backend_census_waits_out_a_resyncing_next_life() -> None:
    # The next life joined at generation+1 but has not finished its boot resync — not ready,
    # so the wait holds rather than returning on a still-resyncing worker.
    stack = _stack([_worker("backend-1", 2, state="resyncing")])
    with pytest.raises(WaitTimeout, match="fresh ready backend-kind life"):
        stack._wait_backend_census(0.2, baseline={"backend-1": 1})


def test_wait_backend_census_without_baseline_accepts_any_ready_backend() -> None:
    # The boot-shaped call (no baseline) passes on any ready backend-kind worker.
    stack = _stack([_worker("backend-1", 1)])
    stack._wait_backend_census(1.0)


def test_wait_backend_census_without_baseline_times_out_with_no_backend() -> None:
    stack = _stack([_worker("serve-1", 1, kind="serve")])
    with pytest.raises(WaitTimeout, match="a ready backend-kind worker"):
        stack._wait_backend_census(0.2)
