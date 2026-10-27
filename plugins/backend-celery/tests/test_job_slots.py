"""The jobs of a worker whose pool cannot end a job at its budget, and the progress they report.

On ``-P solo`` and ``-P threads`` a job runs on until it returns, whatever the task time limit.
A job past ``CELERY_TASK_TIMEOUT`` holds a wedged slot; once every slot is wedged the worker's
tick raises, so the liveness file turns stale. On ``solo`` the job runs on the hub thread, so the
job's own loop ticks the worker's progress while it runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest

from tai42_backend_celery.core import job_slots


def _watch(
    *,
    slots: int,
    budget: float = 60.0,
    ticks_in_job: bool = False,
    tick: Callable[[], None] = lambda: None,
    pool_name: str = "solo",
) -> None:
    job_slots.watch(pool_name=pool_name, slots=slots, budget=budget, ticks_in_job=ticks_in_job, tick=tick)


async def _sleep_then(seconds: float, value: object = None) -> object:
    await asyncio.sleep(seconds)
    return value


def test_with_no_watch_a_job_runs_unchanged() -> None:
    assert asyncio.run(job_slots.in_job(_sleep_then(0, 7), label="tool t1")) == 7
    job_slots.check_wedged()


def test_a_job_past_the_budget_wedges_the_only_solo_slot() -> None:
    _watch(slots=1, budget=0.05)

    async def _job() -> None:
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError) as raised:
            job_slots.check_wedged()
        assert str(raised.value) == (
            "every job slot of this worker holds a job past CELERY_TASK_TIMEOUT=0.05s on the solo pool, which "
            "cannot end a job; reporting stops so the probe ends this worker: tool t1"
        )

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))

    job_slots.check_wedged()


def test_a_job_within_the_budget_wedges_nothing() -> None:
    _watch(slots=1, budget=60.0)

    async def _job() -> None:
        job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))


def test_on_threads_one_overdue_job_beside_a_free_slot_wedges_nothing_and_two_wedge_the_worker() -> None:
    _watch(slots=2, budget=0.05, pool_name="threads")

    async def _first() -> None:
        await asyncio.sleep(0.1)
        job_slots.check_wedged()

    async def _both() -> None:
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match=r"on the threads pool, .*: tool a, tool b$"):
            job_slots.check_wedged()

    async def _main() -> None:
        await job_slots.in_job(_first(), label="tool a")
        await asyncio.gather(
            job_slots.in_job(_both(), label="tool a"),
            job_slots.in_job(_sleep_then(0.15), label="tool b"),
        )

    asyncio.run(_main())


def test_a_job_in_another_process_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    _watch(slots=1, budget=0.0)
    monkeypatch.setattr(job_slots.os, "getpid", lambda: -1)

    async def _job() -> None:
        await asyncio.sleep(0.01)
        job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))


def test_the_job_s_exception_propagates_and_frees_its_slot() -> None:
    _watch(slots=1, budget=0.0, ticks_in_job=True, tick=lambda: None)

    async def _job() -> None:
        raise ValueError("the tool failed")

    with pytest.raises(ValueError, match="the tool failed"):
        asyncio.run(job_slots.in_job(_job(), label="tool t1"))

    job_slots.check_wedged()


def test_on_solo_the_running_job_ticks_the_worker_s_progress_until_it_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    tick_seconds = 0.01
    monkeypatch.setattr(job_slots, "_TICK_SECONDS", tick_seconds)
    ticks: list[str] = []

    async def _main() -> None:
        third_tick = asyncio.Event()

        def _tick() -> None:
            ticks.append("tick")
            if len(ticks) == 3:
                third_tick.set()

        _watch(slots=1, ticks_in_job=True, tick=_tick)

        async def _job() -> str:
            # The job ends once it has seen three ticks; the bound only turns a ticker that
            # stops repeating into a failure instead of a hang.
            await asyncio.wait_for(third_tick.wait(), timeout=30)
            return "done"

        assert await job_slots.in_job(_job(), label="tool t1") == "done"
        assert ticks == ["tick"] * 3
        await asyncio.sleep(5 * tick_seconds)
        assert ticks == ["tick"] * 3

    asyncio.run(_main())


def test_on_threads_the_running_job_leaves_the_ticks_to_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_slots, "_TICK_SECONDS", 0.01)
    ticks: list[str] = []
    _watch(slots=2, ticks_in_job=False, tick=lambda: ticks.append("tick"), pool_name="threads")

    asyncio.run(job_slots.in_job(_sleep_then(0.05), label="tool t1"))

    assert ticks == []


def test_a_tick_that_raises_during_a_job_is_logged_once_and_ends_the_job_s_ticks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(job_slots, "_TICK_SECONDS", 0.01)
    calls: list[str] = []

    def _tick() -> None:
        calls.append("tick")
        raise RuntimeError("the embedded beat (-B) is not alive")

    _watch(slots=1, ticks_in_job=True, tick=_tick)

    with caplog.at_level(logging.ERROR, logger=job_slots.__name__):
        assert asyncio.run(job_slots.in_job(_sleep_then(0.1, "done"), label="tool t1")) == "done"

    assert calls == ["tick"]
    [record] = [r for r in caplog.records if r.name == job_slots.__name__]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "the progress tick during job tool t1 failed; this job reports no more progress"
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "the embedded beat (-B) is not alive"


def test_a_cancelled_job_frees_its_slot_and_ends_its_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_slots, "_TICK_SECONDS", 0.01)
    ticks: list[str] = []
    _watch(slots=1, budget=0.0, ticks_in_job=True, tick=lambda: ticks.append("tick"))

    async def _main() -> None:
        job = asyncio.ensure_future(job_slots.in_job(_sleep_then(10), label="tool t1"))
        await asyncio.sleep(0.03)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        after = len(ticks)
        await asyncio.sleep(0.03)
        assert len(ticks) == after

    asyncio.run(_main())

    job_slots.check_wedged()


def test_a_second_watch_in_the_same_process_keeps_the_running_jobs() -> None:
    _watch(slots=1, budget=0.05)

    async def _job() -> None:
        _watch(slots=1, budget=0.05)
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match="every job slot"):
            job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))
