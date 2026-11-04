"""A job on the gevent pool holds the fork gate's job span, and a re-import tells its own job from the others.

``--pool gevent`` runs each job in the worker process inside the fork gate's job span
(:meth:`~tai42_backend_rq.worker_pools.CustomRQSimpleWorker.execute_job`). A greenlet starts from an
empty context: a re-import the job drives itself (``asyncio.to_thread`` from the job's loop, as the
reload gate runs a re-importing body) carries the job's span mark, so it waits for every other span
open in the process but never for its own. A re-import started on a greenlet of its own during the
job carries no mark, so it waits for the job's span as for any other.

Each case patches a fresh subprocess first, as the pool's process prelude does, builds a real
gevent-pool worker (``_build_worker``) there against a real Redis, runs one job, and prints what
happened; the test process itself is never patched.

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

# The worker process: patch first, as the gevent pool's process prelude does, and bind asyncio's
# future chaining after it, as the host's entry does; bind the suite's stub app (``conftest``);
# build the gevent-pool worker the way ``tai backend worker --pool gevent`` does;
# run the case's job and print what happened. Every wait is bounded: a re-import that waited for its
# own job's span would wait out ``QUIESCE`` and log the ERROR the fork gate reports for it.
_WORKER_SCRIPT = textwrap.dedent(
    """
    from gevent import monkey

    monkey.patch_all()

    from tai42_kit.backend.reshaped_threads import bind_future_chaining

    bind_future_chaining()

    import asyncio
    import json
    import logging
    import sys
    import time

    import gevent
    from gevent.event import Event

    redis_url, tests_dir, case = sys.argv[1:4]
    sys.path.insert(0, tests_dir)
    import conftest  # noqa: F401

    from tai42_kit.fork_gate import fork_gate

    from tai42_backend_rq import worker as worker_module

    QUIESCE = 5.0
    BOUND = 10.0
    HOLD = 0.3


    class _Errors(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())


    errors = _Errors()
    logging.getLogger().addHandler(errors)
    stamps = {}
    greenlets = []


    def rebuild():
        with fork_gate.exclusive(timeout=QUIESCE):
            stamps["rebuild_entered"] = time.monotonic()
            stamps["spans_during_rebuild"] = fork_gate.live_spans


    def own_reload():
        other_held, release_other = Event(), Event()

        def other_span():
            with fork_gate.job_span(timeout=QUIESCE):
                other_held.set()
                release_other.wait(BOUND)

        def release():
            stamps["other_released"] = time.monotonic()
            release_other.set()

        async def reload_from_the_job():
            stamps["spans_before_rebuild"] = fork_gate.live_spans
            gevent.spawn_later(HOLD, release)
            await asyncio.to_thread(rebuild)

        greenlets.append(gevent.spawn(other_span))
        assert other_held.wait(BOUND), "the other greenlet never held its span"
        asyncio.run(reload_from_the_job())


    def reload_during_the_job():
        greenlets.append(gevent.spawn(rebuild))
        time.sleep(HOLD)
        stamps["job_body_ended"] = time.monotonic()
        stamps["entered_during_job"] = "rebuild_entered" in stamps


    def report(*, next_within):
        pass


    worker, connection = worker_module._build_worker(redis_url, None, 500, "gevent", None, report)
    job = worker.queues[0].enqueue(f"__main__.{case}")
    worker.work(max_jobs=1)
    for greenlet in greenlets:
        greenlet.join(BOUND)
    result = job.latest_result()
    print(json.dumps({
        "status": job.get_status(refresh=True).value,
        "exc_string": result.exc_string if result is not None else None,
        "stamps": stamps,
        "errors": errors.messages,
        "spans_after": fork_gate.live_spans,
    }))
    """
)

_TESTS_DIR = Path(__file__).parent


def _run_case(case: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT, _REDIS_URL, str(_TESTS_DIR), case],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "RQ_TASK_TIMEOUT": "60", "RQ_QUEUE_NAME": f"tai-gevent-job-span-{uuid.uuid4().hex}"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_a_re_import_a_job_drives_waits_for_another_span_but_not_its_own() -> None:
    outcome = _run_case("own_reload")

    assert outcome["status"] == "finished", outcome
    stamps = outcome["stamps"]
    assert stamps["spans_before_rebuild"] == 2, "the job and the other greenlet do not hold a span each"
    assert stamps["rebuild_entered"] >= stamps["other_released"], "the re-import did not wait for the other span"
    assert stamps["rebuild_entered"] - stamps["other_released"] < 1.0, "the re-import waited for its own job"
    assert stamps["spans_during_rebuild"] == 1, "the job's own span did not stay open around its re-import"
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0


def test_a_re_import_on_a_greenlet_of_its_own_waits_for_the_running_job() -> None:
    outcome = _run_case("reload_during_the_job")

    assert outcome["status"] == "finished", outcome
    stamps = outcome["stamps"]
    assert stamps["entered_during_job"] is False, "the re-import ran while the job held its span"
    assert stamps["rebuild_entered"] >= stamps["job_body_ended"]
    assert stamps["spans_during_rebuild"] == 0
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0
