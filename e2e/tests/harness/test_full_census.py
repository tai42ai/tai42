"""Harness self-tests for the boot census wait (:func:`wait_full_census`) on a stack
that runs a worker per queue label beside the backend worker.

Every backend process joins the census as a ``backend``-kind row, so the wait keys each
expected process on its own pid: a fleet where only the queue worker has joined is not
ready. These pins drive the wait with a controllable census and stand-in processes, no
infra."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from tai42_e2e.readiness import wait_full_census
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, Topology
from tai42_e2e.variants import BusWorker
from tai42_e2e.waiting import WaitTimeoutError

pytestmark = pytest.mark.needs("no-stack")

_BACKEND_PID = 101
_QUEUE_WORKER_PID = 102
_SERVE_PID = 201


@dataclass
class _Running:
    """A live process as the census wait reads it: its name and pid."""

    name: str
    pid: int

    def is_running(self) -> bool:
        return True


def _row(kind: str, pid: int) -> BusWorker:
    return BusWorker(name=f"{kind}-{pid}", kind=kind, pid=pid, generation=1, joined_at="t", beat_at="t", state="ready")


def _stack(census: list[BusWorker]) -> TaiStack:
    """A bare one-serve-worker stack with the backend worker and the queue ``b`` worker running."""
    stack = object.__new__(TaiStack)
    stack.config = StackConfig(  # type: ignore[misc]
        name="census",
        topology=Topology.MULTIWORKER,
        manifest={},
        env={},
        workers=1,
        run_backend=True,
        queue_workers=("b",),
    )
    stack._procs = {  # type: ignore[attr-defined]
        "backend": _Running("backend", _BACKEND_PID),
        "backend-queue-b": _Running("backend-queue-b", _QUEUE_WORKER_PID),
    }
    stack._supervisor_lock = threading.RLock()  # type: ignore[attr-defined]
    stack._supervised_names = set()  # type: ignore[attr-defined]
    stack.census = lambda: census  # type: ignore[method-assign]
    return stack


def test_the_wait_returns_once_every_backend_process_has_joined() -> None:
    stack = _stack([_row("serve", _SERVE_PID), _row("backend", _BACKEND_PID), _row("backend", _QUEUE_WORKER_PID)])
    wait_full_census(stack, 1.0)


def test_the_wait_holds_while_only_the_queue_worker_has_joined() -> None:
    stack = _stack([_row("serve", _SERVE_PID), _row("backend", _QUEUE_WORKER_PID)])
    with pytest.raises(WaitTimeoutError, match=r"1 serve workers \+ backend \+ backend-queue-b"):
        wait_full_census(stack, 0.2)


def test_a_queue_worker_without_the_backend_worker_is_refused() -> None:
    with pytest.raises(ValueError, match=r"queue_workers .* need run_backend=True"):
        StackConfig(
            name="census",
            topology=Topology.MULTIWORKER,
            manifest={},
            env={},
            run_backend=False,
            queue_workers=("b",),
        )
