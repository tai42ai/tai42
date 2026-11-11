"""``compute_pool`` — a process's own compute pool, started on first use; a forked child of a process that
does not share its pool starts its own."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import multiprocessing
import multiprocessing.process
import operator
import os
import signal
import threading
import time
from collections.abc import Iterator

import pytest

from tai42_kit.compute import ComputePool, ComputePoolClient, compute_pool, stop_compute_pool
from tai42_kit.compute import process_pool as process_pool_module
from tai42_kit.compute.wire_sockets import wire_sockets

_FORK_WARNING = "ignore:This process .* is multi-threaded:DeprecationWarning"


@pytest.fixture(autouse=True)
def _one_worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("COMPUTE_WORKERS", "1")
    stop_compute_pool()  # each test starts with no pool running in the process
    yield
    stop_compute_pool()


def _own_pool() -> ComputePool:
    pool = compute_pool()
    assert isinstance(pool, ComputePool), "a process that was not forked from a sharing process owns its pool"
    return pool


def test_the_first_call_starts_a_pool_sized_by_compute_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTE_WORKERS", "3")

    pool = _own_pool()

    assert pool.workers == 3
    assert asyncio.run(pool.run(operator.add, 1, 2)) == 3


def test_every_call_in_the_process_returns_the_same_pool() -> None:
    assert compute_pool() is compute_pool()


def test_after_a_stop_the_next_call_starts_a_fresh_pool() -> None:
    first = compute_pool()

    stop_compute_pool()
    second = compute_pool()

    assert second is not first
    assert asyncio.run(second.run(operator.add, 2, 2)) == 4


def test_a_pool_whose_workers_died_is_replaced_on_the_next_call(caplog: pytest.LogCaptureFixture) -> None:
    broken = _own_pool()
    os.kill(asyncio.run(broken.run(os.getpid)), signal.SIGKILL)
    deadline = time.monotonic() + 10
    while not broken.broken:
        assert time.monotonic() < deadline, "the library never reported the pool broken"
        time.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="tai42_kit.compute.process_pool"):
        replacement = compute_pool()
        assert asyncio.run(replacement.run(operator.add, 2, 3)) == 5
        assert compute_pool() is replacement

    assert replacement is not broken
    assert [record.getMessage() for record in caplog.records] == ["compute pool replaced after its workers died"]
    with pytest.raises(RuntimeError, match="was stopped"):
        asyncio.run(broken.run(operator.add, 1, 1))


def test_stopping_when_no_pool_runs_does_nothing() -> None:
    stop_compute_pool()
    stop_compute_pool()


def test_a_stopped_pool_still_held_by_a_caller_raises() -> None:
    held = compute_pool()

    stop_compute_pool()

    with pytest.raises(RuntimeError, match="was stopped"):
        asyncio.run(held.run(operator.add, 1, 1))


@pytest.mark.filterwarnings(_FORK_WARNING)
def test_a_forked_child_starts_its_own_pool_and_leaves_the_parents_running() -> None:
    parent_pool = compute_pool()
    parent_worker = asyncio.run(parent_pool.run(os.getpid))
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child, which exits through os._exit
        try:
            os.close(read_fd)
            inherited = process_pool_module._pool
            child_pool = compute_pool()
            child_worker = asyncio.run(child_pool.run(os.getpid))
            stop_compute_pool()
            report = {"inherited": inherited is not None, "own": child_pool is not parent_pool, "worker": child_worker}
            os.write(write_fd, json.dumps(report).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as pipe:
        child = json.loads(pipe.read())
    os.waitpid(pid, 0)

    assert child == {"inherited": False, "own": True, "worker": child["worker"]}
    assert child["worker"] != parent_worker
    assert compute_pool() is parent_pool
    assert asyncio.run(parent_pool.run(os.getpid)) == parent_worker


@pytest.mark.filterwarnings(_FORK_WARNING)
def test_a_forked_child_that_ends_normally_leaves_the_parents_workers_alive() -> None:
    parent_pool = _own_pool()
    parent_worker = asyncio.run(parent_pool.run(os.getpid))
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child, which exits through os._exit
        try:
            os.close(read_fd)
            children = [child.name for child in multiprocessing.active_children()]
            os.write(write_fd, json.dumps(children).encode())
            os.close(write_fd)
            # The exit handlers a normally ending interpreter runs; os._exit keeps the test
            # session's own teardown out of the child.
            atexit._run_exitfuncs()
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as pipe:
        children = json.loads(pipe.read())
    os.waitpid(pid, 0)

    assert children == []
    assert asyncio.run(parent_pool.run(os.getpid)) == parent_worker
    assert not parent_pool.broken


def test_the_fork_hook_forgets_the_inherited_pool_and_lock_and_the_child_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited_lock = threading.Lock()
    inherited_children = {object()}
    monkeypatch.setattr(process_pool_module, "_pool", ComputePool(1))
    monkeypatch.setattr(process_pool_module, "_lock", inherited_lock)
    monkeypatch.setattr(multiprocessing.process, "_children", inherited_children)

    process_pool_module._settle_in_forked_child()

    assert process_pool_module._pool is None
    assert process_pool_module._lock is not inherited_lock
    assert inherited_children == set()
    assert process_pool_module._reached is None


def test_the_fork_hook_reaches_the_pool_over_the_end_the_parent_keeps_in_its_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept, other = wire_sockets.socketpair()
    monkeypatch.setattr(process_pool_module, "_share", object())
    monkeypatch.setattr(process_pool_module, "_reached", None)
    monkeypatch.setattr(process_pool_module, "_pool", None)
    monkeypatch.setattr(process_pool_module, "_lock", threading.Lock())
    monkeypatch.setattr(multiprocessing.process, "_children", set())
    wire_sockets.keep_in_children(kept)
    try:
        process_pool_module._settle_in_forked_child()

        assert process_pool_module._share is None
        reached = process_pool_module._reached
        assert isinstance(reached, ComputePoolClient)
        assert compute_pool() is reached
        reached.close()
        assert wire_sockets.kept_in_children is None
    finally:
        wire_sockets.close(kept)
        wire_sockets.close(other)


@pytest.mark.filterwarnings(_FORK_WARNING)
def test_a_multiprocessing_child_that_never_stops_its_pool_exits() -> None:
    process = multiprocessing.get_context("fork").Process(target=_runs_a_job_and_returns)
    process.start()
    process.join(20)
    try:
        assert process.exitcode == 0, f"the child did not exit within 20s (exit code {process.exitcode})"
    finally:
        if process.exitcode is None:
            process.kill()
            process.join(5)


def _runs_a_job_and_returns() -> None:  # pragma: no cover - runs in the child
    assert asyncio.run(compute_pool().run(operator.add, 1, 2)) == 3
