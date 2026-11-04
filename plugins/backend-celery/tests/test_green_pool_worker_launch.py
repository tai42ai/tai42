"""The worker launch on a green pool, end to end in a fresh process, as ``tai backend worker`` runs it.

The process is patched first by the worker's declared prelude (celery's own patch), binds asyncio's
future chaining as the host's entry does, and is marked as started through it, then runs the
backend's real launch on the standard library's event loop: the kit's prelude check, the plugin's
worker runtime, the Celery CLI with the app's
:class:`~tai42_backend_celery.core.app.HostedWorker`, on an in-memory broker. Two async jobs are
sent at once, then the process signals itself ``SIGTERM``, the stop the host routes to the
worker's warm stop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_LAUNCH = """
import sys

from celery import maybe_patch_concurrency

POOL = sys.argv[1]
maybe_patch_concurrency(["worker", "-P", POOL])

import asyncio
import json
import os
import signal
import threading
import time

from tai42_kit.backend.reshaped_threads import bind_future_chaining

bind_future_chaining()

os.environ["TAI_PROCESS_PRELUDE"] = "celery:maybe_patch_concurrency"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"
sys.path.insert(0, sys.argv[2])
import tests.conftest  # binds the stub app

from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.backend import CeleryBackend
from tai42_backend_celery.core.tasks import AsyncTask

outcome = {}


@celery_app.task(name="green_launch.sleep", base=AsyncTask, bind=True)
def sleep_job(self, seconds):
    async def body():
        await asyncio.sleep(seconds)
        return id(asyncio.get_running_loop())

    return self.run_async(body())


def send_two_jobs_then_stop():
    try:
        deadline = time.monotonic() + 20
        while not celery_app.control.ping(timeout=0.2) and time.monotonic() < deadline:
            pass
        started = time.monotonic()
        jobs = [sleep_job.delay(1.0) for _ in range(2)]
        while not all(job.ready() for job in jobs) and time.monotonic() < deadline:
            time.sleep(0.05)
        outcome["seconds"] = time.monotonic() - started
        outcome["jobs"] = [job.result if job.successful() else repr(job.result) for job in jobs]
    finally:
        os.kill(os.getpid(), signal.SIGTERM)


async def host():
    outcome["host_loop"] = id(asyncio.get_running_loop())
    threading.Thread(target=send_two_jobs_then_stop, daemon=True).start()
    await CeleryBackend().launch(
        ["worker", "-P", POOL, "-c", "2", "--without-gossip", "--without-mingle", "-l", "WARNING"]
    )


try:
    asyncio.run(host())
finally:
    print(json.dumps(outcome), file=sys.__stdout__, flush=True)
"""


@pytest.mark.parametrize("pool_name", ["gevent", "eventlet"])
def test_a_green_pool_worker_runs_its_jobs_on_the_host_s_loop_at_once_and_stops_clean_on_sigterm(
    pool_name: str, tmp_path: Path
) -> None:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(_LAUNCH), pool_name, str(Path(__file__).parents[1])],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**_inherited_env(), "TAI_BACKEND_LIVENESS_PATH": str(tmp_path / "liveness")},
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"the worker process did not exit within 60 s of its stop:\n{exc.stderr}")

    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome["jobs"] == [outcome["host_loop"]] * 2, completed.stderr
    assert outcome["seconds"] < 1.9, "the two one-second jobs did not run at once"
    assert completed.stderr.count("warm shutdown requested (SIGTERM)") == 1, completed.stderr


def _inherited_env() -> dict[str, str]:
    """This process's environment without a process-prelude marker of its own."""
    return {name: value for name, value in os.environ.items() if name != "TAI_PROCESS_PRELUDE"}
