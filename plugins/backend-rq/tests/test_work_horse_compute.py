"""A work horse of a fork-guarded worker runs its compute jobs in the worker's compute pool.

rq forks one work horse per job; the horse runs the job and ends. Its jq evaluation runs in the
pool of the worker that forked it, so the horse starts no compute workers of its own. The worker's
part is played by a forked helper process, so the test session's own process never installs the
guard or shares its pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import time
import traceback
from types import SimpleNamespace

import fakeredis
import pytest
from rq import Queue
from tai42_kit.backend import fork_safety
from tai42_kit.compute import compute_pool, share_compute_pool, stop_compute_pool
from tai42_kit.compute import process_pool as process_pool_module
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_backend_rq import worker_pools

_BOUND = 30.0

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")


@pytest.fixture(autouse=True)
def _one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTE_WORKERS", "1")
    stop_compute_pool()  # the helper process inherits no running pool


def _no_report(*, next_within: float) -> None:
    """A progress reporter that drops every report."""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
def test_a_forked_work_horse_of_a_fork_guarded_worker_evaluates_jq_in_the_workers_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    horse_read, horse_write = os.pipe()

    def in_horse() -> None:  # pragma: no cover - runs in the work horse
        os.close(horse_read)
        _report(horse_write, _evaluates_jq)

    monkeypatch.setattr(fork_safety, "share_compute_pool", share_compute_pool)
    monkeypatch.setattr(worker_pools.Worker, "main_work_horse", lambda self, job, queue: in_horse())
    # rq names the worker and the job in the parent's environment as it forks.
    monkeypatch.setenv("RQ_WORKER_ID", "")
    monkeypatch.setenv("RQ_JOB_ID", "")

    read_fd, write_fd = os.pipe()
    helper = os.fork()
    if helper == 0:  # pragma: no cover - the worker process, which exits through os._exit
        code = 1
        try:
            os.close(read_fd)
            code = _report(write_fd, lambda: _fork_guarded_worker(horse_read, horse_write))
        finally:
            os._exit(code)
    os.close(write_fd)
    os.close(horse_read)
    os.close(horse_write)

    seen = _read_report(read_fd, helper)
    assert _exit_code(helper) == 0
    assert seen == {
        "horse": {"reaches": "ComputePoolClient", "value": [2, 4, 6], "own_pool": False},
        "horse_exit": 0,
    }


def _fork_guarded_worker(horse_read: int, horse_write: int) -> object:  # pragma: no cover - the worker process
    """Guard the worker's forks, run its pool once, then fork a work horse; what the horse saw."""
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    worker = worker_pools.CustomRQWorker([Queue("q", connection=conn)], connection=conn, report_progress=_no_report)
    worker.guard_forks()
    try:
        asyncio.run(compute_pool().run(os.getpid, timeout=_BOUND))
        worker.fork_work_horse(SimpleNamespace(id="j"), "queue")
        os.close(horse_write)
        horse = worker.horse_pid
        seen = _read_report(horse_read, horse)
        return {"horse": seen, "horse_exit": _exit_code(horse)}
    finally:
        stop_compute_pool()


def _evaluates_jq() -> object:  # pragma: no cover - runs in the work horse
    value = asyncio.run(run_jq_first("[.[] * 2]", [1, 2, 3]))
    return {
        "reaches": type(compute_pool()).__name__,
        "value": value,
        "own_pool": process_pool_module._pool is not None,
    }


def _report(write_fd: int, body: object) -> int:  # pragma: no cover - runs in a forked process
    """Run ``body`` and write its JSON value, or its traceback, to ``write_fd``; the exit code to use."""
    try:
        outcome: dict[str, object] = {"value": body()}  # type: ignore[operator]
        code = 0
    except BaseException:  # every failure of the body is the process's report
        outcome = {"error": traceback.format_exc()}
        code = 1
    view = memoryview(json.dumps(outcome).encode())
    while view:
        view = view[os.write(write_fd, view) :]
    os.close(write_fd)
    return code


def _read_report(read_fd: int, pid: int) -> object:
    """The report ``pid`` writes; ``pid`` is killed and the wait fails when none comes within the bound."""
    deadline = time.monotonic() + _BOUND
    chunks: list[bytes] = []
    try:
        while True:
            ready, _, _ = select.select([read_fd], [], [], max(deadline - time.monotonic(), 0))
            if not ready:
                _kill(pid)
                raise AssertionError(f"the process {pid} did not report within {_BOUND}s; it was killed")
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
    outcome = json.loads(b"".join(chunks) or b"{}")
    if "error" in outcome:
        raise AssertionError(f"the process {pid} failed:\n{outcome['error']}")
    if "value" not in outcome:
        raise AssertionError(f"no report from the process {pid}")
    return outcome["value"]


def _exit_code(pid: int) -> int:
    """Reap the forked ``pid``; past the bound it is killed and the wait fails."""
    deadline = time.monotonic() + _BOUND
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    _kill(pid)
    os.waitpid(pid, 0)
    raise AssertionError(f"the process {pid} did not exit within {_BOUND}s; it was killed")


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
