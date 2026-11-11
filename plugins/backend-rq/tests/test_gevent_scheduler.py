"""The gevent pool's worker moves delayed jobs with rq's scheduler running in the worker process.

``tai backend worker --pool gevent`` patches the process before anything else is imported and runs
rq's work loop as a greenlet on the OS thread of the host's event loop. The worker starts rq's
scheduler on a thread of that same process, so the host's loop, its tasks and its Redis sockets stay
the worker's alone. The case builds the gevent-pool runtime that way in a fresh subprocess against a
real Redis, with a host-loop task and a pubsub subscription standing in for the host's own, delays
one job, runs the work loop with rq's scheduler on a worker thread, and prints what happened.

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
# the host's loop, run a ticking task and a pubsub subscription, build the gevent-pool runtime,
# delay one job by a second and run rq's work loop with its scheduler on a worker thread until it
# has performed the job; publish once the scheduler has registered and read the message back on the
# host's loop; print what happened.
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
    from datetime import timedelta

    redis_url, tests_dir, channel = sys.argv[1:4]
    sys.path.insert(0, tests_dir)
    import conftest

    import redis.asyncio as aredis

    from tai42_backend_rq import worker as worker_module

    seen = {"ticks": 0, "tick_failures": []}


    async def where():
        await asyncio.sleep(0)
        return {"pid": os.getpid(), "on_host_loop": asyncio.get_running_loop() is seen["host_loop"]}


    async def ticker():
        while True:
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                seen["tick_failures"].append(f"{type(exc).__name__}: {exc}")
                return
            seen["ticks"] += 1


    async def host():
        seen["host_loop"] = asyncio.get_running_loop()
        ticking = asyncio.create_task(ticker())
        bus = aredis.Redis.from_url(redis_url)
        subscription = bus.pubsub()
        await subscription.subscribe(channel)
        runtime = worker_module.RqWorkerRuntime.from_args(["--pool", "gevent", "--redis-url", redis_url])
        runtime.bind_progress(lambda *, next_within: None)
        await runtime.build()
        worker = runtime._worker
        job = worker.queues[0].enqueue_in(timedelta(seconds=1), "__main__.where")
        thread = threading.Thread(target=worker.work, kwargs={"max_jobs": 1, "with_scheduler": True})
        thread.start()
        deadline = time.monotonic() + 30
        registered = {}
        while not registered and time.monotonic() < deadline:
            if worker.scheduler is not None:
                registered = await bus.hgetall(worker.scheduler.key)
            await asyncio.sleep(0.02)
        await bus.publish(channel, "after-the-scheduler-started")
        received = None
        while received is None and time.monotonic() < deadline:
            message = await subscription.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message is not None:
                received = message["data"].decode()
        while thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        ticking.cancel()
        status = job.get_status(refresh=True).value
        outcome = {
            "worker_pid": os.getpid(),
            "scheduler_pid": int(registered[b"pid"]) if registered else None,
            "work_returned": not thread.is_alive(),
            "job_status": status,
            "job_value": job.return_value() if status == "finished" else None,
            "received": received,
            "ticks": seen["ticks"],
            "tick_failures": seen["tick_failures"],
            "scheduler_key_after_work": bool(await bus.exists(worker.scheduler.key)),
        }
        await subscription.aclose()
        await bus.aclose()
        await runtime.aclose()
        print(json.dumps(outcome), flush=True)


    asyncio.run(host())
    """
)

_TESTS_DIR = Path(__file__).parent


def _run() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT, _REDIS_URL, str(_TESTS_DIR), f"tai-gevent-scheduler-{uuid.uuid4().hex}"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "RQ_QUEUE_NAME": f"tai-gevent-scheduler-{uuid.uuid4().hex}"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "no running event loop" not in completed.stderr, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_a_delayed_job_is_moved_by_a_scheduler_in_the_worker_process_and_the_host_s_loop_runs_on() -> None:
    outcome = _run()

    assert outcome["scheduler_pid"] == outcome["worker_pid"], "rq's scheduler registered from another process"
    assert outcome["work_returned"] is True, "the work loop did not return"
    assert outcome["job_status"] == "finished", outcome
    assert outcome["job_value"] == {"pid": outcome["worker_pid"], "on_host_loop": True}
    assert outcome["received"] == "after-the-scheduler-started", "the host loop's subscription went silent"
    assert outcome["tick_failures"] == []
    assert outcome["ticks"] > 0
    assert outcome["scheduler_key_after_work"] is False, "the scheduler outlived the work loop"
