"""A prefork child of a fork-guarded worker runs its compute jobs in the worker's compute pool.

celery's prefork pool forks its children as daemonic billiard processes, which the standard library
refuses children of their own, so a child cannot start compute workers: its jq evaluation runs in
the pool of the worker that forked it. The worker's part is played by a forked helper process, so
the test session's own process never installs the guard or shares its pool.
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

import pytest
from billiard.context import ForkProcess
from billiard.process import current_process
from tai42_kit.backend import fork_safety
from tai42_kit.compute import share_compute_pool, stop_compute_pool
from tai42_kit.fork_gate import ForkGate
from tai42_kit.utils.data.jq_util import run_jq_first

_BOUND = 30.0

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")


@pytest.fixture(autouse=True)
def _one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTE_WORKERS", "1")
    stop_compute_pool()  # the helper process inherits no running pool


def test_a_daemonic_billiard_child_of_a_fork_guarded_process_evaluates_jq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_safety, "share_compute_pool", share_compute_pool)
    read_fd, write_fd = os.pipe()
    helper = os.fork()
    if helper == 0:  # pragma: no cover - the fork-guarded process, which exits through os._exit
        code = 1
        try:
            os.close(read_fd)
            code = _report(write_fd, _fork_guarded_process)
        finally:
            os._exit(code)
    os.close(write_fd)

    assert _read_report(read_fd, helper) == {"daemon": True, "exit_code": 0, "value": [2, 4, 6]}
    assert _exit_code(helper) == 0


def _fork_guarded_process() -> object:  # pragma: no cover - runs in the forked helper process
    """Guard this process's forks, then evaluate jq in a daemonic billiard child; what the child saw."""
    fork_safety.guard_forks(ForkGate(), timeout=5.0)
    try:
        read_fd, write_fd = os.pipe()

        def child() -> None:
            os.close(read_fd)
            if _report(write_fd, _evaluates_jq) != 0:
                raise SystemExit(1)

        process = ForkProcess(target=child, daemon=True)
        process.start()
        os.close(write_fd)
        assert process.pid is not None
        seen = _read_report(read_fd, process.pid)
        process.join(_BOUND)
        if process.exitcode is None:
            _kill(process.pid)
            process.join(_BOUND)
            raise AssertionError(f"the billiard child {process.pid} did not exit within {_BOUND}s; it was killed")
        return {**seen, "exit_code": process.exitcode}  # type: ignore[dict-item]
    finally:
        stop_compute_pool()


def _evaluates_jq() -> object:  # pragma: no cover - runs in the billiard child
    return {
        "daemon": current_process().daemon,
        "value": asyncio.run(run_jq_first("[.[] * 2]", [1, 2, 3])),
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
