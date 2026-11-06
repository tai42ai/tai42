"""Harness self-tests for the serve/embed restart wait (:func:`tai42_e2e.restarts.wait_after_restart`).

A restarted serve worker answers ``/health`` and MCP requests before it has claimed its
worker-bus slot and left its boot self-resync gate: the claim and the resync run on the bus
subscription task the app spawns at the end of its startup, and the worker's presence row
reads ``ready`` only once its resync returns. The restart wait therefore holds until the
restarted process's fresh lives are on the bus census and READY (keyed on generation, as
the backend restart wait is). These tests drive the wait with a census that lists the fresh
lives only after a few reads and a ``/health`` that answers at once, no infra."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tai42_e2e import readiness, restarts
from tai42_e2e.stack import TaiStack
from tai42_e2e.supervisor import RespawnSupervisor
from tai42_e2e.topology import Topology
from tai42_e2e.variants import BusWorker

pytestmark = pytest.mark.needs("no-stack")


def _worker(name: str, generation: int, *, state: str = "ready", kind: str = "serve", pid: int = 1) -> BusWorker:
    return BusWorker(name=name, kind=kind, pid=pid, generation=generation, joined_at="t", beat_at="t", state=state)


class _Census:
    """A census that answers ``before`` for the first ``reads_before`` reads, then ``after``;
    ``last`` is the rows its latest read answered."""

    def __init__(self, before: list[BusWorker], after: list[BusWorker], *, reads_before: int = 3) -> None:
        self._before = before
        self._after = after
        self._left = reads_before
        self.last: list[BusWorker] | None = None

    def __call__(self) -> list[BusWorker]:
        if self._left > 0:
            self._left -= 1
            self.last = list(self._before)
        else:
            self.last = list(self._after)
        return self.last


def _stack(census: _Census, *, topology: Topology, workers: int, ports: int) -> TaiStack:
    stack = object.__new__(TaiStack)
    stack.procs = {}  # type: ignore[attr-defined]
    stack.supervisor = RespawnSupervisor(stack)
    stack.census = census  # type: ignore[method-assign]
    stack.host = "127.0.0.1"  # type: ignore[attr-defined]
    stack.app_ports = [40001 + i for i in range(ports)]  # type: ignore[attr-defined]
    stack.config = SimpleNamespace(  # type: ignore[attr-defined]
        topology=topology, workers=workers, run_backend=False, path_prefix=""
    )
    stack.infra = SimpleNamespace(settings=SimpleNamespace(boot_timeout=5.0))  # type: ignore[attr-defined]
    return stack


@pytest.fixture(autouse=True)
def _health_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer ``/health`` at once, so each wait below is the census wait alone."""
    monkeypatch.setattr(readiness, "wait_http_ok", lambda *args, **kwargs: None)


def test_serve_restart_returns_only_after_every_fresh_worker_is_on_the_census() -> None:
    # A two-worker master restarted: its old lives left the census, and the respawned
    # workers claim their slots (same names, next generation) only after a few reads.
    fresh = [_worker("serve-1", 2, pid=11), _worker("serve-2", 2, pid=12)]
    census = _Census([], fresh)
    stack = _stack(census, topology=Topology.MULTIWORKER, workers=2, ports=1)
    restarts.wait_after_restart(stack, "serve", {"serve-1": 1, "serve-2": 1})
    assert census.last == fresh


def test_serve_restart_waits_out_a_resyncing_fresh_worker() -> None:
    resyncing = [_worker("serve-1", 2, state="resyncing", pid=11)]
    ready = [_worker("serve-1", 2, pid=11)]
    census = _Census(resyncing, ready)
    stack = _stack(census, topology=Topology.REPLICAS, workers=1, ports=2)
    restarts.wait_after_restart(stack, "serve-a", {"serve-1": 1})
    assert census.last == ready


def test_replica_restart_does_not_count_the_other_replica_as_fresh() -> None:
    # Restarting replica A: replica B's live worker (serve-2) is on the census throughout and
    # is not a fresh life; the wait holds until replica A's worker rejoins as serve-1 gen 2.
    other = _worker("serve-2", 1, pid=22)
    rejoined = [other, _worker("serve-1", 2, pid=11)]
    census = _Census([other], rejoined)
    stack = _stack(census, topology=Topology.REPLICAS, workers=1, ports=2)
    restarts.wait_after_restart(stack, "serve-a", {"serve-1": 1, "serve-2": 1}, {"serve-1"})
    assert census.last == rejoined


def test_embed_restart_returns_only_after_its_fresh_worker_is_on_the_census() -> None:
    fresh = [_worker("serve-1", 2, pid=11)]
    census = _Census([], fresh)
    stack = _stack(census, topology=Topology.MULTIWORKER, workers=2, ports=1)
    stack.config.workers = 1  # type: ignore[attr-defined]
    stack.config.run_backend = True  # type: ignore[attr-defined]
    restarts.wait_after_restart(stack, "embed", {"serve-1": 1})
    assert census.last == fresh
