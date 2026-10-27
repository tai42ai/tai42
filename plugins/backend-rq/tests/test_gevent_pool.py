"""The gevent pool runs each job under its budget and keeps its refresher on an OS thread.

``--pool gevent`` monkey-patches the process, which turns ``threading``'s threads and thread
identities into greenlets: rq's timer penalty cannot target a greenlet, and a refresher greenlet
would neither report progress nor refresh rq's heartbeats while a job holds the CPU. Each case
builds a real gevent-pool worker (``_build_worker``) in a fresh subprocess against a real Redis,
runs one job, and reads what happened.

Skipped when no Redis is reachable — set ``TAI_TEST_RQ_REDIS``
(default ``redis://localhost:6379/15``) to run it.
"""

from __future__ import annotations

import itertools
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

# The worker process: bind the suite's stub app (``conftest``), build the gevent-pool worker the
# way ``tai backend worker --pool gevent`` does, run one job (``spin`` holds the CPU, ``nap``
# sleeps cooperatively) while the worker reports into a liveness file, and print what happened.
_WORKER_SCRIPT = textwrap.dedent(
    """
    import json, logging, sys, time
    from pathlib import Path

    redis_url, tests_dir, liveness_path, body, seconds = sys.argv[1:6]
    sys.path.insert(0, tests_dir)
    import conftest  # noqa: F401

    from tai42_kit.liveness import LivenessFile

    from tai42_backend_rq import worker as worker_module
    from tai42_backend_rq import worker_pools

    window = []


    def spin():
        window.append(time.monotonic())
        end = window[0] + float(seconds)
        while time.monotonic() < end:
            pass
        window.append(time.monotonic())


    def nap():
        window.append(time.monotonic())
        try:
            time.sleep(float(seconds))
        finally:
            window.append(time.monotonic())


    class _Errors(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())


    errors = _Errors()
    logging.getLogger().addHandler(errors)
    liveness = LivenessFile(Path(liveness_path), 25.0)
    reports = []
    heartbeats = []


    def report(*, next_within):
        liveness.report(next_within)
        reports.append(time.monotonic())


    worker_pools._REFRESH_TICK_SECONDS = 0.05
    worker, connection = worker_module._build_worker(redis_url, None, 500, "gevent", None, report)
    worker.job_monitoring_interval = 1
    original_maintain = type(worker).maintain_heartbeats


    def maintain(self, job):
        original_maintain(self, job)
        heartbeats.append(time.monotonic())


    type(worker).maintain_heartbeats = maintain
    job = worker.queues[0].enqueue(f"__main__.{body}")
    worker.work(max_jobs=1)
    start, end = window


    def during(stamps):
        return [stamp - start for stamp in stamps if start <= stamp <= end]


    result = job.latest_result()
    print(json.dumps({
        "status": job.get_status(refresh=True).value,
        "exc_string": result.exc_string if result is not None else None,
        "ran": end - start,
        "reports": during(reports),
        "heartbeats": during(heartbeats),
        "errors": errors.messages,
    }))
    """
)

_TESTS_DIR = Path(__file__).parent


def _run_gevent_job(tmp_path: Path, body: str, seconds: float, budget: int) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _WORKER_SCRIPT,
            _REDIS_URL,
            str(_TESTS_DIR),
            str(tmp_path / "liveness"),
            body,
            str(seconds),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "RQ_TASK_TIMEOUT": str(budget), "RQ_QUEUE_NAME": f"tai-gevent-pool-{uuid.uuid4().hex}"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _largest_gap(stamps: list[float], end: float) -> float:
    """The longest stretch of the job's run, from its start to ``end``, with no stamp in it."""
    edges = [0.0, *sorted(stamps), end]
    return max(later - earlier for earlier, later in itertools.pairwise(edges))


def test_a_cpu_bound_job_keeps_the_refresher_reporting_and_refreshing(tmp_path: Path) -> None:
    outcome = _run_gevent_job(tmp_path, "spin", 3.0, budget=60)

    assert outcome["status"] == "finished", outcome
    ran, reports, heartbeats = outcome["ran"], outcome["reports"], outcome["heartbeats"]
    assert _largest_gap(reports, ran) < 0.5, f"the refresher stopped reporting while the job ran: {reports}"
    assert _largest_gap(heartbeats, ran) < 1.5, f"rq's heartbeats stopped while the job ran: {heartbeats}"
    assert outcome["errors"] == []


def test_a_job_that_yields_is_ended_at_its_budget(tmp_path: Path) -> None:
    outcome = _run_gevent_job(tmp_path, "nap", 4.0, budget=1)

    assert outcome["status"] == "failed", outcome
    assert "JobTimeoutException" in (outcome["exc_string"] or ""), outcome
    assert outcome["ran"] < 1.5, f"the job ran {outcome['ran']:.2f}s on a 1s budget"
    assert [message for message in outcome["errors"] if "exception raised while executing" not in message] == []


def test_a_job_that_holds_the_cpu_past_its_budget_stops_the_reports(tmp_path: Path) -> None:
    """The gevent timeout fires only when the job yields: past its budget, the refresher stops reporting."""
    outcome = _run_gevent_job(tmp_path, "spin", 2.5, budget=1)

    assert outcome["reports"], "the refresher never reported inside the budget"
    assert max(outcome["reports"]) < 1.3, f"reports went on past the budget: {outcome['reports'][-3:]}"
    assert any("is still running past its 1s timeout" in message for message in outcome["errors"]), outcome["errors"]
