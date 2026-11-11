"""A gevent-pool worker process evaluates jq, in a fresh process started through the pool's prelude.

The process is patched by the gevent pool's process prelude, binds asyncio's future chaining as the
host's entry does, and is marked as started through it; it evaluates one jq expression and leaves
through the interpreter. It runs in a process group of its own within a bound, and whatever it left
running is killed with its group.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

_BOUND = 60

_EVALUATE = """
from gevent import monkey

monkey.patch_all()

import asyncio
import json
import os

from tai42_kit.backend.reshaped_threads import bind_future_chaining

bind_future_chaining()
os.environ["TAI_PROCESS_PRELUDE"] = "gevent.monkey:patch_all"
os.environ["COMPUTE_WORKERS"] = "1"

from tai42_kit.utils.data.jq_util import run_jq_first

print(json.dumps(asyncio.run(run_jq_first("[.[] * 2]", [1, 2, 3]))), flush=True)
"""


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_gevent_pool_worker_process_evaluates_jq_and_leaves_nothing_running() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_EVALUATE)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        env={name: value for name, value in os.environ.items() if name != "TAI_PROCESS_PRELUDE"},
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=_BOUND)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(f"the gevent pool process did not finish within {_BOUND} s:\n{stdout}\n{stderr}")

        assert process.returncode == 0, stderr
        assert json.loads(stdout.strip().splitlines()[-1]) == [2, 4, 6], stderr
        deadline = time.monotonic() + 5
        while _group_alive(process.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _group_alive(process.pid), "the process's compute pool outlived it"
    finally:
        if _group_alive(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
