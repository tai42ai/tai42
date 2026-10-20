"""The multi-worker supervisor over real worker processes: each death is logged with its reason and status.

A driver process serves a minimal ASGI factory through :func:`run_workers` with two
workers. One worker is sent SIGSEGV from outside (it exits by itself; the fault
handler armed through the serve fault-handler seam prints its traceback), the other
is SIGSTOPped (it stops answering the health ping and the supervisor kills it). The
driver's log must name each case.

Each worker makes itself non-dumpable before serving, so the crash ends the worker at
once whatever the host's ``core_pattern`` is: a worker caught dumping core is the unit
tests' to cover, and this test does not depend on how long a host takes to dump.
The driver runs in its own session, so the cleanup reaches its workers through the
driver's process group and nothing else.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals (SIGSEGV/SIGSTOP) drive the workers")

_DEADLINE = 120.0
_HEALTHCHECK_TIMEOUT = 5

_APP_MODULE = """
import ctypes
import os
import sys

from tai42_skeleton.cli.mcp_app import _enable_fault_handler

_PR_SET_DUMPABLE = 4


def _never_dump_core():
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")
    else:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def create():
    _never_dump_core()
    _enable_fault_handler()

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    return app
"""

_DRIVER = """
from tai42_skeleton.cli.worker_supervisor import run_workers

if __name__ == "__main__":
    run_workers(
        "supervised_app:create",
        workers=2,
        factory=True,
        host="127.0.0.1",
        port=0,
        lifespan="on",
        timeout_worker_healthcheck={timeout},
    )
"""

_STARTED = re.compile(r"Started server process \[(\d+)\]")


def _wait_for(log: Path, predicate, what: str) -> str:
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:
        text = log.read_text(errors="replace")
        if predicate(text):
            return text
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}; log so far:\n{log.read_text(errors='replace')}")


def test_worker_deaths_are_logged_with_reason_and_status(tmp_path: Path) -> None:
    (tmp_path / "supervised_app.py").write_text(textwrap.dedent(_APP_MODULE))
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(_DRIVER).replace("{timeout}", str(_HEALTHCHECK_TIMEOUT)))
    log = tmp_path / "serve.log"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")])}
    env.pop("PYTHONFAULTHANDLER", None)

    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, str(driver)],
            cwd=tmp_path,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        text = _wait_for(log, lambda t: t.count("Application startup complete.") >= 2, "both workers to start")
        crashed, hung = (int(pid) for pid in _STARTED.findall(text)[:2])

        os.kill(crashed, signal.SIGSEGV)
        crash_line = f"Child process [{crashed}] died (exited by itself): killed by signal SIGSEGV (-11)"
        text = _wait_for(log, lambda t: crash_line in t, "the crashed worker's death line")
        assert "Fatal Python error: Segmentation fault" in text

        os.kill(hung, signal.SIGSTOP)
        hang_line = (
            f"Child process [{hung}] died (did not answer the health check within "
            f"{_HEALTHCHECK_TIMEOUT}s and was killed): killed by signal SIGKILL (-9)"
        )
        text = _wait_for(log, lambda t: hang_line in t, "the hung worker's death line")

        # Each death is followed by a replacement worker starting.
        _wait_for(log, lambda t: len(_STARTED.findall(t)) >= 4, "two replacement workers to start")
        assert "WARNING" in next(line for line in text.splitlines() if crash_line in line)
    finally:
        # A clean stop joins every worker. Otherwise the driver is still unreaped, so its
        # process group id cannot have been reused, and the group holds only its workers.
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
