"""The celery worker's progress step: one report per tick of the consumer's hub timer.

The step is a consumer bootstep, so celery registers its timer entry again on every consumer
start, including the one after a broker reconnect clears the hub's timer entries.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from celery.concurrency import solo, thread
from celery.worker.consumer.connection import Connection
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
    """The consumer a step starts on: its timer and its pool."""

    def __init__(self, *, pool: Any = None) -> None:
        self.timer = _Timer()
        self.pool = pool if pool is not None else SimpleNamespace(limit=4)


def test_register_adds_the_step_to_the_app_s_consumer_blueprint() -> None:
    report = _Reports()

    liveness_step.register(celery_app, report, task_timeout=300)

    assert liveness_step.ProgressStep in liveness_step.consumer_steps(celery_app)
    liveness_step._tick()
    assert report.promises == [liveness_step._PROGRESS_TICK_SECONDS]


def test_a_second_register_with_the_same_reporter_changes_nothing() -> None:
    report = _Reports()
    liveness_step.register(celery_app, report, task_timeout=300)

    liveness_step.register(celery_app, report, task_timeout=300)

    liveness_step._tick()
    assert report.promises == [1.0]


def test_a_second_register_with_another_reporter_raises() -> None:
    liveness_step.register(celery_app, _Reports(), task_timeout=300)

    with pytest.raises(RuntimeError, match="already reports its progress through another reporter"):
        liveness_step.register(celery_app, _Reports(), task_timeout=300)


def test_a_tick_with_no_reporter_raises() -> None:
    with pytest.raises(RuntimeError, match="ticked with no reporter registered"):
        liveness_step._tick()


def test_the_step_runs_once_the_broker_connection_is_up() -> None:
    # A worker's blueprint rewrites ``requires`` into a list of the resolved steps when it loads.
    assert list(liveness_step.ProgressStep.requires) == [Connection]


def test_start_registers_a_repeating_tick_and_stop_cancels_it() -> None:
    consumer = _Consumer()
    step = liveness_step.ProgressStep(consumer)

    step.start(consumer)
    [entry] = consumer.timer.entries
    assert (entry.secs, entry.fun) == (1.0, liveness_step._tick)

    step.stop(consumer)
    assert entry.canceled is True
    step.stop(consumer)


def test_a_restarted_consumer_registers_exactly_one_new_tick() -> None:
    consumer = _Consumer()
    step = liveness_step.ProgressStep(consumer)

    step.start(consumer)
    step.shutdown(consumer)
    step.start(consumer)

    first, second = consumer.timer.entries
    assert first.canceled is True
    assert second.canceled is False


async def test_the_worker_runtime_registers_its_reporter_at_build(monkeypatch: pytest.MonkeyPatch) -> None:
    import tai42_backend_celery.core.backend as backend_module

    monkeypatch.setattr(backend_module.prefork, "register", lambda: None)
    monkeypatch.setattr(backend_module.sys, "argv", list(backend_module.sys.argv))
    runtime = CeleryWorkerRuntime.from_args([])
    report = _Reports()
    runtime.bind_progress(report)

    await runtime.build()

    assert liveness_step.ProgressStep in liveness_step.consumer_steps(celery_app)
    liveness_step._tick()
    assert report.promises == [1.0]


# -- the composed tick -----------------------------------------------------------


def _started(consumer: _Consumer, *, task_timeout: float = 300) -> _Reports:
    report = _Reports()
    liveness_step.register(celery_app, report, task_timeout=task_timeout)
    liveness_step.ProgressStep(consumer).start(consumer)
    return report


def test_a_prefork_or_green_pool_counts_no_job_slots() -> None:
    _started(_Consumer(pool=SimpleNamespace(limit=4)))

    assert job_slots._watch is None


def test_a_solo_pool_counts_its_one_slot_and_ticks_during_a_job() -> None:
    _started(_Consumer(pool=solo.TaskPool(limit=1)), task_timeout=120)

    watch = job_slots._watch
    assert watch is not None
    assert (watch.pool_name, watch.slots, watch.budget, watch.ticks_in_job) == ("solo", 1, 120, True)
    assert watch.tick is liveness_step._tick


def test_a_threads_pool_counts_its_slots_and_leaves_the_ticks_to_the_hub() -> None:
    pool = thread.TaskPool(limit=3)
    try:
        _started(_Consumer(pool=pool), task_timeout=120)
    finally:
        pool.executor.shutdown()

    watch = job_slots._watch
    assert watch is not None
    assert (watch.pool_name, watch.slots, watch.budget, watch.ticks_in_job) == ("threads", 3, 120, False)


def test_a_tick_withholds_the_report_while_the_embedded_beat_is_not_alive() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    embedded_beat.watch(SimpleNamespace(pid=4242))
    report = _started(_Consumer())

    with pytest.raises(RuntimeError, match=r"^the embedded beat \(-B\) is not alive: no liveness file at "):
        liveness_step._tick()

    assert report.promises == []


def test_a_tick_withholds_the_report_once_every_slot_is_wedged() -> None:
    report = _started(_Consumer(pool=solo.TaskPool(limit=1)), task_timeout=0.05)

    async def _job() -> None:
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match="every job slot of this worker holds a job past"):
            liveness_step._tick()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))

    assert report.promises == [1.0]
    liveness_step._tick()
    assert report.promises == [1.0, 1.0]
