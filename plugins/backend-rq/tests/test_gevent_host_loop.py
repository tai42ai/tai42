"""The gevent pool runs every job's coroutine on the host's event loop.

``tai backend worker --pool gevent`` patches the process before anything else is imported, runs the
host's event loop on the main greenlet, and runs rq's work loop on a thread of its own, which the
patch makes a greenlet of the same OS thread. asyncio runs one loop per OS thread, so a job's
coroutine cannot run on a loop of its own there: it runs on the host's, handed to the jobs by the
runtime's build. Each case builds the runtime that way in a fresh subprocess against a real Redis,
runs its jobs on a worker thread while the host's loop runs, and prints what happened.

Skipped when no Redis is reachable — set ``TAI_TEST_RQ_REDIS``
(default ``redis://localhost:6379/15``) to run it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest
from redis import Redis

_REDIS_URL = os.environ.get("TAI_TEST_RQ_REDIS", "redis://localhost:6379/15")


def _redis_reachable(url: str) -> bool:
    try:
        Redis.from_url(url).ping()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _redis_reachable(_REDIS_URL), reason=f"no Redis reachable at {_REDIS_URL}")

# The worker process: patch first and name the prelude, as the gevent pool's process prelude does, and bind asyncio's
# future chaining after it, as the host's entry does; bind the suite's stub app (``conftest``); on
# the host's loop, build the gevent-pool runtime, enqueue the case's jobs and run rq's work loop on
# a worker thread until it has performed them; print what happened.
_WORKER_SCRIPT = textwrap.dedent(
    """
    from gevent import monkey

    monkey.patch_all()

    from tai42_kit.backend.reshaped_threads import bind_future_chaining

    bind_future_chaining()

    import os

    os.environ["TAI_PROCESS_PRELUDE"] = "gevent.monkey:patch_all"

    import asyncio
    import json
    import sys
    import threading
    import time

    redis_url, tests_dir, case = sys.argv[1:4]
    sys.path.insert(0, tests_dir)
    import conftest

    from tai42_kit.clients.base import PooledClient
    from tai42_kit.loop_bound import release_loop_bound_resources
    from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

    from tai42_backend_rq import worker as worker_module
    from tai42_backend_rq.settings import rq_settings

    os_thread = monkey.get_original("_thread", "get_ident")
    seen = {}


    async def where():
        await asyncio.sleep(0)
        return {"on_host_loop": asyncio.get_running_loop() is seen["host_loop"], "os_thread": os_thread()}


    async def overruns():
        started = time.monotonic()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen["overrun_cancelled_after"] = time.monotonic() - started
            raise
        finally:
            seen["cleanup_on_host_loop"] = asyncio.get_running_loop() is seen["host_loop"]


    async def catches_its_budget():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen["budget_caught_after"] = time.monotonic()
        try:
            while "stop_taken" not in seen:
                await asyncio.sleep(0.01)
        finally:
            seen["cleanup_on_host_loop"] = asyncio.get_running_loop() is seen["host_loop"]


    class _Connection:
        closed = False


    class _Client(PooledClient):
        async def _create(self, **kwargs):
            return _Connection()

        async def _close(self, client):
            client.closed = True


    client = _Client()


    async def leases_a_pooled_client(key, arguments, **kwargs):
        async with client.current(name="harness") as connection:
            seen["connection"] = connection
        return "leased"


    conftest.stub_app.tools.run_tool = leases_a_pooled_client
    tool_job = {rq_settings().tool_name_arg: "harness_tool", WORKER_SECRET_CAPABILITY_ARG: False}
    CASES = {
        "where": [("__main__.where", {})],
        "timeout": [("__main__.overruns", {}), ("__main__.where", {})],
        "pooled": [("tai42_backend_rq.tasks.tool_execution", tool_job)],
        "stop": [("__main__.overruns", {}), ("__main__.where", {})],
        "budget_then_stop": [("__main__.catches_its_budget", {}), ("__main__.where", {})],
    }


    # Record the OS thread and the hub on which rq's stop-job listener asks the worker to stop its job.
    def records_the_stop(worker):
        import gevent

        kill_horse = worker.kill_horse

        def kill_horse_recorded(*args, **kwargs):
            seen["stop_os_thread"] = os_thread()
            seen["stop_on_the_job_s_hub"] = gevent.get_hub() is seen["hub"]
            return kill_horse(*args, **kwargs)

        worker.kill_horse = kill_horse_recorded


    # Record whether each stop the worker makes reaches a section of the job, and that the stop was taken.
    def records_the_stop_s_reach(worker):
        from tai42_backend_rq.death_penalties import WatchedDeathPenalty

        interrupt = WatchedDeathPenalty.interrupt

        def interrupt_recorded(job_id):
            seen.setdefault("stop_reached", []).append(interrupt(job_id))
            return seen["stop_reached"][-1]

        WatchedDeathPenalty.interrupt = interrupt_recorded
        kill_horse = worker.kill_horse

        def kill_horse_taken(*args, **kwargs):
            try:
                return kill_horse(*args, **kwargs)
            finally:
                seen["stop_taken"] = True

        worker.kill_horse = kill_horse_taken


    async def stop_once_started(worker, job):
        from rq.command import send_stop_job_command
        from rq.job import JobStatus

        while job.get_status(refresh=True) != JobStatus.STARTED:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        send_stop_job_command(worker.connection, job.id)


    async def stop_once_the_budget_is_caught(worker, job):
        from rq.command import send_stop_job_command

        while "budget_caught_after" not in seen:
            await asyncio.sleep(0.01)
        seen["stop_sent_after_the_catch"] = time.monotonic() - seen["budget_caught_after"]
        send_stop_job_command(worker.connection, job.id)


    async def host():
        import gevent

        seen["host_loop"] = asyncio.get_running_loop()
        seen["hub"] = gevent.get_hub()
        runtime = worker_module.RqWorkerRuntime.from_args(["--pool", "gevent", "--redis-url", redis_url])
        runtime.bind_progress(lambda *, next_within: None)
        await runtime.build()
        worker = runtime._worker
        jobs = [worker.queues[0].enqueue(func, kwargs=kwargs) for func, kwargs in CASES[case]]
        if case == "stop":
            records_the_stop(worker)
        if case == "budget_then_stop":
            records_the_stop_s_reach(worker)
        thread = threading.Thread(target=worker.work, kwargs={"max_jobs": len(jobs)})
        thread.start()
        if case == "stop":
            await stop_once_started(worker, jobs[0])
        if case == "budget_then_stop":
            await stop_once_the_budget_is_caught(worker, jobs[0])
        while thread.is_alive():
            await asyncio.sleep(0.01)
        outcome = {"host_os_thread": os_thread(), "jobs": []}
        for job in jobs:
            result = job.latest_result()
            status = job.get_status(refresh=True).value
            outcome["jobs"].append({
                "status": status,
                "exc_string": result.exc_string if result is not None else None,
                "value": job.return_value() if status == "finished" else None,
            })
        outcome["cleanup_on_host_loop"] = seen.get("cleanup_on_host_loop")
        outcome["overrun_cancelled_after"] = seen.get("overrun_cancelled_after")
        outcome["stop_os_thread"] = seen.get("stop_os_thread")
        outcome["stop_on_the_job_s_hub"] = seen.get("stop_on_the_job_s_hub")
        outcome["budget_caught"] = "budget_caught_after" in seen
        outcome["stop_reached"] = seen.get("stop_reached")
        if "connection" in seen:
            outcome["open_after_the_job"] = not seen["connection"].closed
            await release_loop_bound_resources()
            outcome["closed_after_the_host_release"] = seen["connection"].closed
        await runtime.aclose()
        print(json.dumps(outcome))


    asyncio.run(host())
    """
)

_TESTS_DIR = Path(__file__).parent


def _run_case(case: str, budget: int = 60) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT, _REDIS_URL, str(_TESTS_DIR), case],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "RQ_TASK_TIMEOUT": str(budget), "RQ_QUEUE_NAME": f"tai-gevent-host-loop-{uuid.uuid4().hex}"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_an_async_job_runs_on_the_host_s_loop_on_its_os_thread() -> None:
    outcome = _run_case("where")

    [job] = outcome["jobs"]
    assert job["status"] == "finished", job["exc_string"]
    assert job["value"] == {"on_host_loop": True, "os_thread": outcome["host_os_thread"]}


def test_a_job_past_its_budget_is_cancelled_on_the_host_s_loop_and_the_worker_runs_the_next() -> None:
    outcome = _run_case("timeout", budget=1)

    overrun, following = outcome["jobs"]
    assert overrun["status"] == "failed", overrun
    assert "JobTimeoutException" in overrun["exc_string"], overrun["exc_string"]
    assert "Task exceeded maximum timeout value (1 seconds)" in overrun["exc_string"], overrun["exc_string"]
    assert outcome["cleanup_on_host_loop"] is True, "the job's cleanup did not run on the host's loop"
    cancelled_after = outcome["overrun_cancelled_after"]
    assert cancelled_after is not None, "the job's task on the host's loop was not cancelled"
    assert cancelled_after < 1 + 2, f"the job's task was cancelled {cancelled_after:.1f}s in, past its 1s budget"
    assert following["status"] == "finished", following["exc_string"]
    assert following["value"]["on_host_loop"] is True


def test_a_stop_ends_a_running_job_on_the_host_s_loop_and_the_worker_runs_the_next() -> None:
    """rq's stop-job listener is a greenlet on the job's own OS thread and hub; the stop is thrown
    into the job's greenlet, its wait cancels the job's task on the host's loop, and rq marks it stopped."""
    outcome = _run_case("stop")

    stopped, following = outcome["jobs"]
    assert outcome["stop_os_thread"] == outcome["host_os_thread"], "rq's stop-job listener ran on another OS thread"
    assert outcome["stop_on_the_job_s_hub"] is True, "rq's stop-job listener ran on another hub"
    assert stopped["status"] == "stopped", stopped
    assert "JobStopped: job " in stopped["exc_string"], stopped["exc_string"]
    assert "stopped by RQ's stop-job command" in stopped["exc_string"], stopped["exc_string"]
    assert "JobTimeoutException" not in stopped["exc_string"], stopped["exc_string"]
    assert outcome["cleanup_on_host_loop"] is True, "the job's cleanup did not run on the host's loop"
    cancelled_after = outcome["overrun_cancelled_after"]
    assert cancelled_after is not None, "the job's task on the host's loop was not cancelled"
    assert cancelled_after < 5, f"the job's task was cancelled {cancelled_after:.1f}s in, not at the stop"
    assert following["status"] == "finished", following["exc_string"]
    assert following["value"]["on_host_loop"] is True


def test_a_job_that_caught_its_budget_s_cancel_takes_no_stop_and_is_marked_stopped_as_it_fails() -> None:
    """The budget's timer closes the job's section as it throws, so a stop sent once the job's coroutine
    has caught the cancel throws nothing into the job. The stop's mark stays: the job fails on its budget
    once its coroutine returns, rq marks it stopped, and the worker runs the next."""
    outcome = _run_case("budget_then_stop", budget=1)

    stopped, following = outcome["jobs"]
    assert outcome["budget_caught"] is True, "the job's coroutine was not cancelled at its budget"
    assert outcome["stop_reached"] == [False], outcome["stop_reached"]
    assert stopped["status"] == "stopped", stopped
    assert "JobTimeoutException" in stopped["exc_string"], stopped["exc_string"]
    assert "JobStopped" not in stopped["exc_string"], stopped["exc_string"]
    assert outcome["cleanup_on_host_loop"] is True, "the job's cleanup did not run on the host's loop"
    assert following["status"] == "finished", following["exc_string"]


def test_a_job_leaves_the_host_s_pooled_clients_to_the_host_s_release() -> None:
    outcome = _run_case("pooled")

    [job] = outcome["jobs"]
    assert job["status"] == "finished", job["exc_string"]
    assert job["value"] == "leased"
    assert outcome["open_after_the_job"] is True, "the job released the host loop's pooled clients"
    assert outcome["closed_after_the_host_release"] is True
