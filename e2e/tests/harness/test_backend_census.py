"""Harness self-tests for the backend-census restart wait
(:meth:`TaiStack._wait_backend_census`).

A restarted backend worker joins no port — its readiness keys on a ``backend``-kind
census origin. The SIGKILLed worker's presence key lingers until its heartbeat TTL,
so a restart must wait for a GENUINELY NEW origin, not pass on the corpse. These
pins drive the wait with a controllable census, no infra."""

from __future__ import annotations

import pytest

from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import BusOrigin
from tai42_e2e.waiting import WaitTimeout


def _stack(census: list[BusOrigin]) -> TaiStack:
    """A bare stack whose census returns a fixed list and whose early-exit check
    is a no-op (no live processes to inspect)."""
    stack = object.__new__(TaiStack)
    stack._procs = {}  # type: ignore[attr-defined]
    stack.census = lambda: census  # type: ignore[method-assign]
    return stack


def test_wait_backend_census_ignores_the_lingering_pre_restart_origin() -> None:
    old = BusOrigin(origin="backend-old", kind="backend", pid=1)
    stack = _stack([old])
    # Only the corpse is present, so the restart wait must NOT pass on it.
    with pytest.raises(WaitTimeout, match="new backend-kind origin"):
        stack._wait_backend_census(0.2, exclude={old.origin})


def test_wait_backend_census_returns_on_a_new_backend_origin() -> None:
    old = BusOrigin(origin="backend-old", kind="backend", pid=1)
    new = BusOrigin(origin="backend-new", kind="backend", pid=2)
    serve = BusOrigin(origin="serve-x", kind="serve", pid=3)
    # The corpse still lingers beside a fresh worker and the serve fleet; the wait
    # keys on the new backend origin.
    stack = _stack([old, new, serve])
    stack._wait_backend_census(1.0, exclude={old.origin})


def test_wait_backend_census_without_exclude_accepts_any_backend_origin() -> None:
    # The boot-shaped call (empty exclude) still passes on any backend origin.
    stack = _stack([BusOrigin(origin="backend-1", kind="backend", pid=1)])
    stack._wait_backend_census(1.0)


def test_wait_backend_census_without_exclude_times_out_with_no_backend() -> None:
    stack = _stack([BusOrigin(origin="serve-x", kind="serve", pid=1)])
    with pytest.raises(WaitTimeout, match="a backend-kind origin"):
        stack._wait_backend_census(0.2)
