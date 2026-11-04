"""The celery worker's progress step: one report per tick of the consumer's hub timer.

The step is a consumer bootstep, so celery registers its timer entry again on every consumer
start, including the one after a broker reconnect clears the hub's timer entries.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from celery.concurrency import prefork, solo, thread
from celery.worker.consumer.connection import Connection
from tai42_kit.backend import fork_safety
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate
from tai42_kit.liveness import backend_liveness_settings

from tai42_backend_celery.core import embedded_beat, job_slots, liveness_step
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.backend import CeleryWorkerRuntime


class _Reports:
    def __init__(self) -> None:
        self.promises: list[float] = []

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)


class _Entry:
    def __init__(self, secs: float, fun: Any) -> None:
        self.secs = secs
        self.fun = fun
        self.canceled = False

    def cancel(self) -> None:
        self.canceled = True


class _Timer:
    """The consumer's timer, recording each repeating entry."""

    def __init__(self) -> None:
        self.entries: list[_Entry] = []

    def call_repeatedly(self, secs: float, fun: Any) -> _Entry:
        entry = _Entry(secs, fun)
        self.entries.append(entry)
        return entry


class _Consumer:
    """The consumer a step starts on: its app, its timer and its pool."""

    def __init__(self, *, pool: Any = None) -> None:
        self.app = celery_app
        self.timer = _Timer()
        self.pool = pool if pool is not None else prefork.TaskPool(limit=4)


def test_register_adds_the_step_to_the_app_s_consumer_blueprint() -> None:
    report = _Reports()

    liveness_step.register(celery_app, report, task_timeout=300)

    assert liveness_step.ProgressStep in liveness_step.consumer_steps(celery_app)
    liveness_step.tick(celery_app)
    assert report.promises == [job_slots.PROGRESS_TICK_SECONDS]


def test_a_second_register_with_the_same_reporter_changes_nothing() -> None:
    report = _Reports()
    liveness_step.register(celery_app, report, task_timeout=300)

    liveness_step.register(celery_app, report, task_timeout=300)

    liveness_step.tick(celery_app)
    assert report.promises == [1.0]


def test_a_second_register_with_another_reporter_raises() -> None:
    liveness_step.register(celery_app, _Reports(), task_timeout=300)

    with pytest.raises(RuntimeError, match="already reports its progress through another reporter"):
        liveness_step.register(celery_app, _Reports(), task_timeout=300)


def test_a_tick_with_no_reporter_raises() -> None:
    with pytest.raises(RuntimeError, match="ticked with no reporter registered"):
        liveness_step.tick(celery_app)


def test_the_step_runs_once_the_broker_connection_is_up() -> None:
    # A worker's blueprint rewrites ``requires`` into a list of the resolved steps when it loads.
    assert list(liveness_step.ProgressStep.requires) == [Connection]


def test_start_registers_a_repeating_tick_and_stop_cancels_it() -> None:
    consumer = _Consumer()
    step = liveness_step.ProgressStep(consumer)

    liveness_step.register(celery_app, _Reports(), task_timeout=300)
    step.start(consumer)
    [entry] = consumer.timer.entries
    assert entry.secs == 1.0
    assert entry.fun.func is liveness_step.tick
    assert entry.fun.args == (celery_app,)

    step.stop(consumer)
    assert entry.canceled is True
    step.stop(consumer)


def test_start_on_an_app_with_no_progress_registered_raises() -> None:
    consumer = _Consumer()

    with pytest.raises(RuntimeError, match="ticked with no reporter registered"):
        liveness_step.ProgressStep(consumer).start(consumer)

    assert consumer.timer.entries == []


def test_a_restarted_consumer_registers_exactly_one_new_tick() -> None:
    consumer = _Consumer()
    step = liveness_step.ProgressStep(consumer)
    liveness_step.register(celery_app, _Reports(), task_timeout=300)

    step.start(consumer)
    step.shutdown(consumer)
    step.start(consumer)

    first, second = consumer.timer.entries
    assert first.canceled is True
    assert second.canceled is False


async def test_the_worker_runtime_registers_its_reporter_at_build(monkeypatch: pytest.MonkeyPatch) -> None:
    import tai42_backend_celery.core.backend as backend_module

    monkeypatch.setattr(backend_module.prefork.PoolTurnover, "register", lambda self: None)
    runtime = CeleryWorkerRuntime.from_args([])
    report = _Reports()
    runtime.bind_progress(report)

    await runtime.build()

    assert liveness_step.ProgressStep in liveness_step.consumer_steps(celery_app)
    liveness_step.tick(celery_app)
    assert report.promises == [1.0]


async def test_the_worker_build_installs_the_fork_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import tai42_backend_celery.core.backend as backend_module

    guards: list[dict[str, Any]] = []
    monkeypatch.setattr(backend_module.prefork.PoolTurnover, "register", lambda self: None)
    monkeypatch.setattr(backend_module.fork_gate, "guard_forks", lambda **kwargs: guards.append(kwargs))
    runtime = CeleryWorkerRuntime.from_args([])
    runtime.bind_progress(_Reports())

    await runtime.build()

    assert guards == [{"timeout": JOB_SPAN_WAIT_SECONDS, "after_in_child": fork_safety.evict_inherited_monitoring}]


async def test_the_worker_runs_its_hub_as_the_fork_gate_s_engine_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The celery CLI, and with it the hub loop, runs inside the engine loop on the run body's thread."""
    import tai42_backend_celery.core.backend as backend_module

    loops: list[Any] = []
    monkeypatch.setattr(
        backend_module, "_run_celery_command", lambda *_: loops.append(fork_gate._engine_loop.tick_here())
    )
    runtime = CeleryWorkerRuntime.from_args([])

    runtime.run_blocking()

    [loop] = loops
    assert loop is not None
    assert (loop.tick.func, loop.tick.args) == (liveness_step.tick, (celery_app,))
    assert loop.tick_every == job_slots.PROGRESS_TICK_SECONDS
    assert fork_gate._engine_loop.tick_here() is None


def _fork_an_exiting_child() -> None:
    """Fork a child that exits at once, and reap it."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os._exit(0)
    os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
async def test_a_fork_waiting_off_the_hub_thread_reports_no_pass_of_the_hub_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork on a thread that is not the hub's waits on the re-import, and reports nothing for the hub loop."""
    import tai42_backend_celery.core.backend as backend_module

    monkeypatch.setattr(backend_module.prefork.PoolTurnover, "register", lambda self: None)
    runtime = CeleryWorkerRuntime.from_args([])
    report = _Reports()
    runtime.bind_progress(report)
    await runtime.build()
    held, release = threading.Event(), threading.Event()

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=5.0):
            held.set()
            release.wait(10.0)

    rebuilder = threading.Thread(target=rebuild, daemon=True)
    rebuilder.start()
    assert held.wait(5.0)
    with liveness_step.hub_as_engine_loop(celery_app):
        forker = threading.Thread(target=_fork_an_exiting_child, daemon=True)
        forker.start()
        time.sleep(2.5)
        release.set()
        forker.join(10.0)
    rebuilder.join(10.0)

    assert not forker.is_alive()
    assert report.promises == []


# -- the composed tick -----------------------------------------------------------


def _started(consumer: _Consumer, *, task_timeout: float = 300) -> _Reports:
    report = _Reports()
    liveness_step.register(celery_app, report, task_timeout=task_timeout)
    liveness_step.ProgressStep(consumer).start(consumer)
    return report


def _pool_from(module: str, *, is_green: bool = False) -> Any:
    """A pool whose class lives in ``module``, as celery's own pool classes each do, green or not."""
    return type("TaskPool", (), {"__module__": module, "is_green": is_green})()


@pytest.mark.parametrize(
    ("module", "watched"),
    [
        ("celery.concurrency.prefork", ("prefork", False, False, 120, False)),
        ("celery.concurrency.solo", ("solo", True, False, 120, True)),
        ("celery.concurrency.thread", ("threads", True, False, 120, False)),
        ("celery.concurrency.gevent", ("gevent", True, True, 120, False)),
        ("celery.concurrency.eventlet", ("eventlet", True, True, 120, False)),
    ],
)
def test_each_celery_pool_is_classified(module: str, watched: tuple[str, bool, bool, float, bool]) -> None:
    is_green = importlib.import_module(module).TaskPool.is_green
    _started(_Consumer(pool=_pool_from(module, is_green=is_green)), task_timeout=120)

    watch = celery_app.job_watch
    assert watch is not None
    assert (watch.pool_name, watch.in_process, watch.green, watch.budget, watch.ticks_in_job) == watched
    assert watch.tick.func is liveness_step.tick


@pytest.mark.parametrize("is_green", [True, False])
def test_the_watched_pool_is_green_as_the_pool_itself_says(is_green: bool) -> None:
    _started(_Consumer(pool=_pool_from("celery.concurrency.thread", is_green=is_green)))

    watch = celery_app.job_watch
    assert watch is not None
    assert watch.green is is_green


def test_an_unknown_pool_raises_at_start() -> None:
    liveness_step.register(celery_app, _Reports(), task_timeout=120)
    consumer = _Consumer(pool=_pool_from("acme.pools"))

    with pytest.raises(RuntimeError) as raised:
        liveness_step.ProgressStep(consumer).start(consumer)

    assert str(raised.value) == (
        "the celery worker's pool TaskPool from acme.pools is not one celery ships "
        "(prefork, solo, threads, gevent, eventlet); its progress cannot be reported"
    )
    assert consumer.timer.entries == []


def test_a_solo_pool_is_held_to_the_budget_and_ticks_during_a_job() -> None:
    _started(_Consumer(pool=solo.TaskPool(limit=1)), task_timeout=120)

    watch = celery_app.job_watch
    assert watch is not None
    assert (watch.pool_name, watch.in_process, watch.budget, watch.ticks_in_job) == ("solo", True, 120, True)


def test_a_threads_pool_leaves_the_ticks_to_the_hub() -> None:
    pool = thread.TaskPool(limit=3)
    try:
        _started(_Consumer(pool=pool), task_timeout=120)
    finally:
        pool.executor.shutdown()

    watch = celery_app.job_watch
    assert watch is not None
    assert (watch.pool_name, watch.in_process, watch.budget, watch.ticks_in_job) == ("threads", True, 120, False)


def test_a_tick_withholds_the_report_while_the_embedded_beat_is_not_alive() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    embedded_beat.watch(celery_app, SimpleNamespace(pid=4242))
    report = _started(_Consumer())

    with pytest.raises(RuntimeError, match=r"^the embedded beat \(-B\) is not alive: no liveness file at "):
        liveness_step.tick(celery_app)

    assert report.promises == []


def test_a_tick_withholds_the_report_once_a_slot_is_wedged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_slots, "PROGRESS_TICK_SECONDS", 0.01)
    report = _started(_Consumer(pool=solo.TaskPool(limit=1)), task_timeout=0.05)

    async def _job() -> None:
        await asyncio.sleep(0.1)
        reported = len(report.promises)
        with pytest.raises(RuntimeError, match="a job slot of this worker holds a job that still runs a tick past"):
            liveness_step.tick(celery_app)
        assert len(report.promises) == reported

    asyncio.run(job_slots.in_job(celery_app, _job(), label="tool t1"))

    reported = len(report.promises)
    liveness_step.tick(celery_app)
    assert report.promises[reported:] == [job_slots.PROGRESS_TICK_SECONDS]
