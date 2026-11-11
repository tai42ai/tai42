"""Run a function in a forked child process and read back what it returned, within a bound.

A child reports its function's JSON-encodable return value, or the traceback it raised, over a
pipe and exits through ``os._exit`` (a ``multiprocessing`` child through its own exit), so the
test session's teardown never runs in it. A child that does not report within its bound is
killed and the wait fails, so a hang fails the test instead of holding the session.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import select
import signal
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

BOUND = 30.0


@dataclass
class ForkedChild:
    """A forked child that runs one function and reports what it returned."""

    pid: int
    _report: int
    _reap: Callable[[float], int | None]

    def result(self, timeout: float = BOUND) -> object:
        """What the child's function returned; raises ``AssertionError`` with its traceback when it raised."""
        value = read_report(self._report, self.pid, timeout)
        exit_code = self._reap(timeout)
        assert exit_code == 0, f"the forked child {self.pid} exited with {exit_code}"
        return value


def fork_child(body: Callable[[], object]) -> ForkedChild:
    """Run ``body`` in a child made by a plain ``os.fork``."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child, which exits through os._exit
        code = 1
        try:
            os.close(read_fd)
            code = report_to(write_fd, body)
        finally:
            os._exit(code)
    os.close(write_fd)
    return ForkedChild(pid, read_fd, lambda timeout: wait_exit(pid, timeout))


def fork_daemonic_child(body: Callable[[], object]) -> ForkedChild:
    """Run ``body`` in a daemonic ``multiprocessing`` child started by ``fork``."""
    read_fd, write_fd = os.pipe()

    def target() -> None:  # pragma: no cover - runs in the child
        os.close(read_fd)
        if report_to(write_fd, body) != 0:
            raise SystemExit(1)

    process = multiprocessing.get_context("fork").Process(target=target, daemon=True)
    process.start()
    os.close(write_fd)

    def reap(timeout: float) -> int | None:
        process.join(timeout)
        if process.exitcode is None:
            process.kill()
            process.join(timeout)
            raise AssertionError(f"the daemonic child {process.pid} did not exit within {timeout}s; it was killed")
        return process.exitcode

    assert process.pid is not None
    return ForkedChild(process.pid, read_fd, reap)


def in_forked_process(body: Callable[[], object], timeout: float = BOUND) -> object:
    """Run ``body`` in a plain forked child and return what it returned."""
    return fork_child(body).result(timeout)


def report_to(write_fd: int, body: Callable[[], object]) -> int:
    """Run ``body`` and write what it returned, or its traceback, to ``write_fd``; return the exit code to use."""
    try:
        outcome: dict[str, object] = {"value": body()}
        code = 0
    except BaseException:  # every failure of the body is the child's report
        outcome = {"error": traceback.format_exc()}
        code = 1
    data = json.dumps(outcome).encode()
    view = memoryview(data)
    while view:
        view = view[os.write(write_fd, view) :]
    os.close(write_fd)
    return code


def read_report(read_fd: int, pid: int, timeout: float = BOUND) -> object:
    """Read the report written to the other end of ``read_fd``; kill ``pid`` and fail when none comes in time."""
    report = _read_within(read_fd, pid, timeout)
    if not report:
        raise AssertionError(f"no report from the forked process {pid}")
    outcome = json.loads(report)
    if "error" in outcome:
        raise AssertionError(f"the forked process {pid} failed:\n{outcome['error']}")
    return outcome["value"]


def _read_within(read_fd: int, pid: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    try:
        while True:
            left = deadline - time.monotonic()
            ready, _, _ = select.select([read_fd], [], [], max(left, 0))
            if not ready:
                _kill(pid)
                raise AssertionError(f"the forked child {pid} did not report within {timeout}s; it was killed")
            chunk = os.read(read_fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(read_fd)


def wait_exit(pid: int, timeout: float = BOUND) -> int:
    """Reap the child ``pid`` and return its exit code (negative: the signal that ended it); killed past ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    _kill(pid)
    os.waitpid(pid, 0)
    raise AssertionError(f"the forked child {pid} did not exit within {timeout}s; it was killed")


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
