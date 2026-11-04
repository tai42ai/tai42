"""A real celery worker on a pool that runs its jobs in the worker process shuts their work down once as it stops.

Each case runs in a fresh process, as ``tai backend worker`` runs it: patched first by celery's own
patch for the pool (the launch's prelude on a green pool), with asyncio's future chaining bound as
the host's entry binds it, the worker's receivers are connected and its progress registered for its
app as the worker's build does, the host's event loop is handed to the jobs and the worker runs off
it. The worker, on an in-memory broker, runs one async job of its app's ``tool_execution`` and then
stops warm.
The process prints how often the monitoring spans were flushed and a loop's loop-bound kit
resources released: those of a job's own loop are the worker's to release, those of the host's
loop the host's.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_WORKER = """
import sys

from celery import maybe_patch_concurrency

maybe_patch_concurrency(["worker", "-P", sys.argv[1]])

import asyncio
import json
import threading
import time

from tai42_kit.backend.reshaped_threads import bind_future_chaining

bind_future_chaining()

sys.path.insert(0, sys.argv[2])
import tests.conftest as conftest

from celery.worker import state

from tai42_backend_celery.core import liveness_step, worker_signals
from tai42_backend_celery.core.app import HostedCelery
from tai42_backend_celery.core.tasks import TOOL_EXECUTION_TASK, AsyncTask

app = HostedCelery("warm-stop", broker="memory://", backend="cache+memory://")
app.conf.broker_transport_options = {"polling_interval": 0.05}
tool_execution = app.tasks[TOOL_EXECUTION_TASK]


@app.task(name="warm_stop.job")
def job():
    tool_execution.push_request(id="warm-stop-job")
    try:
        return tool_execution.run_async(asyncio.sleep(0, result="ran"))
    finally:
        tool_execution.pop_request()


worker_signals.connect(app)
liveness_step.register(app, lambda next_within: None, task_timeout=60.0)
worker = app.Worker(
    pool=sys.argv[1],
    concurrency=2,
    without_gossip=True,
    without_mingle=True,
    without_heartbeat=True,
    loglevel="ERROR",
    quiet=True,
)
results = []


def run_one_job_then_stop():
    result = job.delay()
    deadline = time.monotonic() + 10
    while not result.ready() and time.monotonic() < deadline:
        time.sleep(0.05)
    results.append(result.result if result.ready() else "the job did not finish")
    state.should_stop = 0


async def host():
    AsyncTask.share_loop(app, asyncio.get_running_loop())
    threading.Thread(target=run_one_job_then_stop, daemon=True).start()
    await asyncio.to_thread(worker.start)


asyncio.run(host())
stub = conftest.stub_app_instance
# celery sends the worker's stdout into its logging; the outcome goes to the real stdout.
print(
    json.dumps(
        {
            "job": results,
            "flushes": stub.monitoring.active.writer.flush.call_count,
            "loop_releases": conftest.loop_release_recorder.calls,
        }
    ),
    file=sys.__stdout__,
    flush=True,
)
"""


@pytest.mark.parametrize(("pool_name", "releases"), [("solo", 1), ("threads", 1), ("gevent", 0), ("eventlet", 0)])
def test_a_warm_stop_shuts_the_jobs_work_down_once(pool_name: str, releases: int) -> None:
    script = textwrap.dedent(_WORKER)
    completed = subprocess.run(
        [sys.executable, "-c", script, pool_name, str(Path(__file__).parents[1])],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome == {"job": ["ran"], "flushes": 1, "loop_releases": releases}, completed.stderr
