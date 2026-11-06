"""Harness self-tests for the boot census wait (:func:`wait_full_census`) on a stack
that runs a worker per queue label, or a worker per green pool, beside the backend worker.

Every backend process joins the census as a ``backend``-kind row, so the wait keys each
expected process on its own pid: a fleet where only the queue worker has joined is not
ready, nor is one whose green-pool worker has not joined. A serve worker counts only once
its own presence row reads ``ready``, and a wait that times out names each worker that is
missing or not ready. These pins drive the wait with a controllable census and stand-in
processes, no infra."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from tai42_e2e.readiness import expected_backend_processes, expected_fleet_present, wait_full_census
from tai42_e2e.stack import TaiStack
from tai42_e2e.supervisor import RespawnSupervisor
from tai42_e2e.topology import StackConfig, Topology
from tai42_e2e.variants import BACKENDS, BusWorker
from tai42_e2e.waiting import WaitTimeoutError

pytestmark = pytest.mark.needs("no-stack")

_BACKEND_PID = 101
_QUEUE_WORKER_PID = 102
_GEVENT_WORKER_PID = 103
_EVENTLET_WORKER_PID = 104
_SERVE_PID = 201


@dataclass
class _Running:
    """A live process as the census wait reads it: its name and pid."""

    name: str
    pid: int

    def is_running(self) -> bool:
        return True


def _row(kind: str, pid: int, *, state: str = "ready") -> BusWorker:
    return BusWorker(name=f"{kind}-{pid}", kind=kind, pid=pid, generation=1, joined_at="t", beat_at="t", state=state)


def _resyncing_serve_rows() -> list[BusWorker]:
    """The full fleet of :func:`_stack` with its serve worker's row still ``resyncing``."""
    return [
        _row("serve", _SERVE_PID, state="resyncing"),
        _row("backend", _BACKEND_PID),
        _row("backend", _QUEUE_WORKER_PID),
    ]


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
    stack.procs = {  # type: ignore[attr-defined]
        "backend": _Running("backend", _BACKEND_PID),
        "backend-queue-b": _Running("backend-queue-b", _QUEUE_WORKER_PID),
    }
    stack.supervisor = RespawnSupervisor(stack)
    stack.census = lambda: census  # type: ignore[method-assign]
    return stack


def _green_pool_stack(census: list[BusWorker]) -> TaiStack:
    """A bare one-serve-worker celery stack with the backend worker and a worker per green pool running."""
    stack = object.__new__(TaiStack)
    stack.config = StackConfig(  # type: ignore[misc]
        name="census",
        topology=Topology.MULTIWORKER,
        manifest={},
        env={},
        workers=1,
        run_backend=True,
        green_pool_workers=True,
    )
    stack.infra = SimpleNamespace(variants=SimpleNamespace(backend=BACKENDS["celery"]))  # type: ignore[assignment]
    stack.procs = {  # type: ignore[attr-defined]
        "backend": _Running("backend", _BACKEND_PID),
        "backend-gevent": _Running("backend-gevent", _GEVENT_WORKER_PID),
        "backend-eventlet": _Running("backend-eventlet", _EVENTLET_WORKER_PID),
    }
    stack.supervisor = RespawnSupervisor(stack)
    stack.census = lambda: census  # type: ignore[method-assign]
    return stack


def test_the_wait_returns_once_every_backend_process_has_joined() -> None:
    stack = _stack([_row("serve", _SERVE_PID), _row("backend", _BACKEND_PID), _row("backend", _QUEUE_WORKER_PID)])
    wait_full_census(stack, 1.0)


def test_the_wait_holds_while_only_the_queue_worker_has_joined() -> None:
    stack = _stack([_row("serve", _SERVE_PID), _row("backend", _QUEUE_WORKER_PID)])
    with pytest.raises(
        WaitTimeoutError,
        match=r"1 ready serve workers \+ backend \+ backend-queue-b\): backend process backend \(pid 101\) has no",
    ):
        wait_full_census(stack, 0.2)


def test_the_wait_names_the_serve_worker_still_resyncing() -> None:
    stack = _stack(_resyncing_serve_rows())
    with pytest.raises(WaitTimeoutError, match=r"serve worker serve-201 \(pid 201\) is resyncing"):
        wait_full_census(stack, 0.2)


def test_the_wait_counts_the_serve_workers_with_no_row() -> None:
    stack = _stack([_row("backend", _BACKEND_PID), _row("backend", _QUEUE_WORKER_PID)])
    with pytest.raises(WaitTimeoutError, match=r"1 of 1 serve workers have no presence row"):
        wait_full_census(stack, 0.2)


def test_the_fleet_is_present_with_every_serve_worker_and_backend_process_by_pid() -> None:
    rows = [_row("serve", _SERVE_PID), _row("backend", _BACKEND_PID), _row("backend", _QUEUE_WORKER_PID)]
    assert expected_fleet_present(_stack(rows), rows)


def test_the_fleet_is_not_present_while_a_serve_worker_is_resyncing() -> None:
    # Its row is on the census, but its on-ready self-resync (and with it the boot gate) has not returned.
    rows = _resyncing_serve_rows()
    assert not expected_fleet_present(_stack(rows), rows)


def test_the_fleet_is_not_present_while_a_backend_process_is_missing_from_the_census() -> None:
    # A backend being respawned has no row; the rows that are present may all be ready.
    rows = [_row("serve", _SERVE_PID), _row("backend", _QUEUE_WORKER_PID)]
    assert not expected_fleet_present(_stack(rows), rows)


def test_the_backend_processes_include_a_worker_per_green_pool() -> None:
    assert expected_backend_processes(_green_pool_stack([])) == ["backend", "backend-gevent", "backend-eventlet"]


def test_the_fleet_is_not_present_while_a_green_pool_worker_is_missing_from_the_census() -> None:
    rows = [_row("serve", _SERVE_PID), _row("backend", _BACKEND_PID), _row("backend", _GEVENT_WORKER_PID)]
    assert not expected_fleet_present(_green_pool_stack(rows), rows)


def test_the_fleet_is_present_once_every_green_pool_worker_has_joined() -> None:
    rows = [
        _row("serve", _SERVE_PID),
        _row("backend", _BACKEND_PID),
        _row("backend", _GEVENT_WORKER_PID),
        _row("backend", _EVENTLET_WORKER_PID),
    ]
    assert expected_fleet_present(_green_pool_stack(rows), rows)


def test_the_fleet_is_not_present_without_its_serve_worker() -> None:
    rows = [_row("backend", _BACKEND_PID), _row("backend", _QUEUE_WORKER_PID)]
    assert not expected_fleet_present(_stack(rows), rows)


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


def test_liveness_processes_without_the_backend_worker_are_refused() -> None:
    with pytest.raises(ValueError, match=r"liveness_processes need run_backend=True"):
        StackConfig(
            name="census",
            topology=Topology.MULTIWORKER,
            manifest={},
            env={},
            run_backend=False,
            liveness_processes=True,
        )


def test_green_pool_workers_without_the_backend_worker_are_refused() -> None:
    with pytest.raises(ValueError, match=r"green_pool_workers need run_backend=True"):
        StackConfig(
            name="census",
            topology=Topology.MULTIWORKER,
            manifest={},
            env={},
            run_backend=False,
            green_pool_workers=True,
        )
