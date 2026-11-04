"""A config reload re-imports this plugin's whole package between a runtime's build and its run.

The host re-imports the package named as its ``backend_module`` on every epoch build, the
first-connect resync before the worker's boot-ready latch included, so the package's import-time
registrations re-fire into each epoch. Each test here builds a runtime, re-imports the package the
way the host does (every module of it popped from ``sys.modules``, then imported again), and then
runs the runtime: the worker runs jobs on the worker, the job class and the penalty records its
build made, over an in-process fake Redis server, and the beat runs the scheduler and queue class
its build took and reports through the reporter it was handed.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys
import time
from collections.abc import Callable, Iterator
from types import ModuleType

import fakeredis
import pytest
from rq import Queue, get_current_job
from rq.job import Job, JobStatus
from rq_scheduler import Scheduler

import tai42_backend_rq
from tai42_backend_rq import beat, job_loop, worker_pools
from tai42_backend_rq import worker as worker_module
from tai42_backend_rq.backend import RqBeatRuntime
from tai42_backend_rq.death_penalties import WatchedDeathPenalty
from tai42_backend_rq.job_loop import HostLoopJob, PrivateLoopJob
from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.settings import rq_settings
from tai42_backend_rq.worker import RqWorkerRuntime

from .conftest import StubApp

_PACKAGE = tai42_backend_rq.__name__
_RUNS_WITHIN = 10.0


def _package_module_names() -> list[str]:
    """Every module of the package, the package root first, as the host's re-import walks it."""
    return [_PACKAGE] + [info.name for info in pkgutil.walk_packages(tai42_backend_rq.__path__, prefix=f"{_PACKAGE}.")]


@pytest.fixture
def reimport_package(app: StubApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], ModuleType]]:
    """Re-import the plugin's package as the host's reload does; the test process gets its own copies back after.

    The re-import registers into the stub app, as it does into the host's; what it registered is
    undone with ``sys.modules``.
    """
    names = _package_module_names()
    kept_modules = {name: module for name, module in sys.modules.items() if name.split(".")[0] == _PACKAGE}
    monkeypatch.setattr(app.tools, "registered", dict(app.tools.registered))
    monkeypatch.setattr(app.tools, "tags", dict(app.tools.tags))
    monkeypatch.setattr(app.extensions, "registered", list(app.extensions.registered))
    monkeypatch.setattr(app.extensions, "factories", dict(app.extensions.factories))
    monkeypatch.setattr(app.backends, "registered", list(app.backends.registered))

    def _reimport() -> ModuleType:
        for name in [name for name in sys.modules if name.split(".")[0] == _PACKAGE]:
            del sys.modules[name]
        for name in names:
            importlib.import_module(name)
        fresh = sys.modules[_PACKAGE]
        assert fresh is not tai42_backend_rq
        return fresh

    yield _reimport

    for name in [name for name in sys.modules if name.split(".")[0] == _PACKAGE]:
        del sys.modules[name]
    sys.modules.update(kept_modules)


def _fresh(module: ModuleType) -> ModuleType:
    """The re-imported copy of one of the package's modules."""
    fresh = sys.modules[module.__name__]
    assert fresh is not module
    return fresh


def reports_its_job() -> dict[str, int | bool]:
    """A job: the identity of the class of the job object running it, and whether a stop of it
    reaches its guarded section through the build's penalty records.

    Identities, as the result is pickled, and a class pickles by its name, which the re-imported
    module now answers to.
    """
    job = get_current_job()
    assert job is not None
    return {
        "job_class": id(type(job)),
        "stoppable": job.id in WatchedDeathPenalty._stoppable,
    }


async def reports_its_loop() -> int:
    """A coroutine job: the identity of the loop it runs on."""
    return id(asyncio.get_running_loop())


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeStrictRedis:
    """The in-memory server's connection, the one the worker runtime's build connects with.

    rq's scheduler is not started: the worker's jobs are what the tests read. An idle worker sees a
    drain within a tenth of a second.
    """
    monkeypatch.setattr(worker_pools, "_DEQUEUE_POLL_SECONDS", 0.1)
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    monkeypatch.setattr(worker_module, "rq_redis_connection", lambda redis_url: conn)
    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "_start_scheduler", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(worker_pools.CustomRQGeventWorker, "_start_scheduler", lambda self, *args, **kwargs: None)
    return conn


async def _built_worker(pool: str) -> RqWorkerRuntime:
    runtime = RqWorkerRuntime.from_args(["--pool", pool])
    runtime.bind_progress(lambda *, next_within: None)
    await runtime.build()
    return runtime


async def _work_until_done(runtime: RqWorkerRuntime, *jobs: Job) -> None:
    """Run the runtime's work loop off the host's loop, as the host runs it, until ``jobs`` end; then drain it."""
    working = asyncio.ensure_future(asyncio.to_thread(runtime.run_blocking))
    deadline = time.monotonic() + _RUNS_WITHIN
    while any(job.get_status(refresh=True) not in (JobStatus.FINISHED, JobStatus.FAILED) for job in jobs):
        assert not working.done(), "the work loop ended before the job did"
        assert time.monotonic() < deadline, "the job never ended"
        await asyncio.sleep(0.02)
    runtime.request_drain()
    await asyncio.wait_for(working, _RUNS_WITHIN)


async def test_after_a_reimport_the_solo_worker_runs_its_jobs_on_what_its_build_made(
    reimport_package: Callable[[], ModuleType], connection: fakeredis.FakeStrictRedis
) -> None:
    runtime = await _built_worker("solo")
    built = runtime._worker
    try:
        reimport_package()
        assert type(built) is worker_pools.CustomRQSimpleWorker
        assert type(built) is not _fresh(worker_pools).CustomRQSimpleWorker

        job = Queue(rq_settings().queue_name, connection=connection).enqueue(reports_its_job)
        await _work_until_done(runtime, job)
    finally:
        await runtime.aclose()

    assert runtime._worker is built
    assert job.get_status(refresh=True) == JobStatus.FINISHED
    assert job.return_value() == {"job_class": id(PrivateLoopJob), "stoppable": True}


async def test_after_a_reimport_a_gevent_job_runs_on_the_loop_the_build_handed(
    reimport_package: Callable[[], ModuleType], connection: fakeredis.FakeStrictRedis
) -> None:
    runtime = await _built_worker("gevent")
    try:
        reimport_package()
        assert _fresh(job_loop).HostLoopJob._shared_loop is None

        queue = Queue(rq_settings().queue_name, connection=connection)
        job = queue.enqueue(reports_its_job)
        coroutine_job = queue.enqueue(reports_its_loop)
        await _work_until_done(runtime, job, coroutine_job)
    finally:
        await runtime.aclose()

    assert job.get_status(refresh=True) == JobStatus.FINISHED
    assert job.return_value() == {"job_class": id(HostLoopJob), "stoppable": True}
    assert coroutine_job.get_status(refresh=True) == JobStatus.FINISHED
    assert coroutine_job.return_value() == id(asyncio.get_running_loop())


async def test_after_a_reimport_the_beat_runs_what_its_build_took_and_reports_through_its_reporter(
    reimport_package: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    promises: list[float] = []
    runtime = RqBeatRuntime.from_args(["-i", "12"])
    runtime.bind_progress(lambda *, next_within: promises.append(next_within))
    ran: list[Scheduler] = []

    def _one_pass(self: Scheduler, burst: bool = False) -> None:
        ran.append(self)
        self.heartbeat()

    monkeypatch.setattr(Scheduler, "run", _one_pass)
    monkeypatch.setattr(Scheduler, "heartbeat", lambda self: None)
    monkeypatch.setattr(beat, "setup_loghandlers", lambda level: None)

    await runtime.build()
    reimport_package()
    runtime.run_blocking()

    [scheduler] = ran
    assert type(scheduler) is beat.ProgressReportingScheduler
    assert scheduler.queue_class is TaskBudgetQueue
    assert promises == [12.0]
