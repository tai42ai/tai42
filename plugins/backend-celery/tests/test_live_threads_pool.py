"""Live verification: async task bodies run side by side on a ``threads`` pool.

Each job of an :class:`~tai42_backend_celery.core.tasks.AsyncTask` runs its coroutine on an event
loop; on ``-P threads`` several jobs of the one task run at once, each on its own pool thread.

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run it (see the live worker harness module).
"""

from __future__ import annotations

import time

from ._live_redis_worker import _BROKER, _app_on, _live_marks, _result

pytestmark = _live_marks()


def test_two_async_jobs_run_at_once_on_two_threads(monkeypatch, run_worker) -> None:
    app = _app_on(monkeypatch, _BROKER)
    run_worker(app, pool="threads", concurrency=2)
    assert _result(app, timeout=30) == 5

    started = time.monotonic()
    jobs = [app.send_task("tests.live_async_sleep", args=(2.0,)) for _ in range(2)]
    results = [job.get(timeout=30, disable_sync_subtasks=False) for job in jobs]

    assert results == [2.0, 2.0]
    assert time.monotonic() - started < 3.5, "the two jobs did not run at once"
