"""Harness self-tests for the serve/embed restart wait (:meth:`TaiStack._wait_after_restart`).

A restarted serve worker answers ``/health`` and MCP requests before it has claimed its
worker-bus slot: the claim runs on the bus subscription task the app spawns at the end of
its startup. A probe that reads the worker's bus identity in that window fails with a
non-retriable error, so the restart wait must hold until the restarted process's fresh
lives are on the bus census (keyed on generation, as the backend restart wait is) before
it drains the boot gate. These tests drive the wait with a census that lists the fresh
lives only after a few reads and a ``/health`` that answers at once, no infra."""

from __future__ import annotations

import threading
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from tai42_e2e import readiness
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import Topology
from tai42_e2e.variants import BusWorker

pytestmark = pytest.mark.needs("no-stack")


def _worker(name: str, generation: int, *, state: str = "ready", kind: str = "serve", pid: int = 1) -> BusWorker:
    return BusWorker(name=name, kind=kind, pid=pid, generation=generation, joined_at="t", beat_at="t", state=state)


class _Census:
    """A census that answers ``before`` for the first ``reads_before`` reads, then ``after``."""

    def __init__(self, before: list[BusWorker], after: list[BusWorker], *, reads_before: int = 3) -> None:
        self._before = before
        self._after = after
        self._left = reads_before

    def __call__(self) -> list[BusWorker]:
        if self._left > 0:
            self._left -= 1
            return list(self._before)
        return list(self._after)


def _stack(census: _Census, *, topology: Topology, workers: int, ports: int) -> TaiStack:
    stack = object.__new__(TaiStack)
    stack._procs = {}  # type: ignore[attr-defined]
    stack._supervisor_lock = threading.RLock()  # type: ignore[attr-defined]
    stack._supervised_names = set()  # type: ignore[attr-defined]
    stack.census = census  # type: ignore[method-assign]
    stack.host = "127.0.0.1"  # type: ignore[attr-defined]
    stack.app_ports = [40001 + i for i in range(ports)]  # type: ignore[attr-defined]
    stack.config = SimpleNamespace(  # type: ignore[attr-defined]
        topology=topology, workers=workers, run_backend=False, path_prefix=""
    )
    stack.infra = SimpleNamespace(settings=SimpleNamespace(boot_timeout=5.0))  # type: ignore[attr-defined]
    return stack


@pytest.fixture
def drained(monkeypatch: pytest.MonkeyPatch) -> list[list[BusWorker]]:
    """Answer ``/health`` at once and record the census each time the boot-gate drain starts."""
    seen: list[list[BusWorker]] = []
    stacks: list[TaiStack] = []

    def fake_drain(stack: TaiStack, app_ports: list[int], deadline: float) -> Coroutine[Any, Any, None]:
        stacks.append(stack)

        async def noop() -> None:
            return None

        return noop()

    def fake_run(coro: Coroutine[Any, Any, None]) -> None:
        coro.close()
        seen.append(stacks[-1].census())

    monkeypatch.setattr(readiness, "wait_http_ok", lambda *args, **kwargs: None)
    monkeypatch.setattr(readiness, "drain_gate_coro", fake_drain)
    monkeypatch.setattr(readiness, "run_readiness_coro", fake_run)
    return seen


def test_serve_restart_drains_only_after_every_fresh_worker_is_on_the_census(
    drained: list[list[BusWorker]],
) -> None:
    # A two-worker master restarted: its old lives left the census, and the respawned
    # workers claim their slots (same names, next generation) only after a few reads.
    fresh = [_worker("serve-1", 2, pid=11), _worker("serve-2", 2, pid=12)]
    stack = _stack(_Census([], fresh), topology=Topology.MULTIWORKER, workers=2, ports=1)
    stack._wait_after_restart("serve", {"serve-1": 1, "serve-2": 1})
    assert drained == [fresh]


def test_serve_restart_waits_out_a_resyncing_fresh_worker(drained: list[list[BusWorker]]) -> None:
    resyncing = [_worker("serve-1", 2, state="resyncing", pid=11)]
    ready = [_worker("serve-1", 2, pid=11)]
    stack = _stack(_Census(resyncing, ready), topology=Topology.REPLICAS, workers=1, ports=2)
    stack._wait_after_restart("serve-a", {"serve-1": 1})
    assert drained == [ready]


def test_replica_restart_does_not_count_the_other_replica_as_fresh(drained: list[list[BusWorker]]) -> None:
    # Restarting replica A: replica B's live worker (serve-2) is on the census throughout and
    # is not a fresh life; the wait holds until replica A's worker rejoins as serve-1 gen 2.
    other = _worker("serve-2", 1, pid=22)
    rejoined = [other, _worker("serve-1", 2, pid=11)]
    stack = _stack(_Census([other], rejoined), topology=Topology.REPLICAS, workers=1, ports=2)
    stack._wait_after_restart("serve-a", {"serve-1": 1, "serve-2": 1}, {"serve-1"})
    assert drained == [rejoined]


def test_embed_restart_drains_only_after_its_fresh_worker_is_on_the_census(
    drained: list[list[BusWorker]],
) -> None:
    fresh = [_worker("serve-1", 2, pid=11)]
    stack = _stack(_Census([], fresh), topology=Topology.MULTIWORKER, workers=2, ports=1)
    stack.config.workers = 1  # type: ignore[attr-defined]
    stack.config.run_backend = True  # type: ignore[attr-defined]
    stack._wait_after_restart("embed", {"serve-1": 1})
    assert drained == [fresh]
