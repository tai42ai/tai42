"""The jobs running in a worker process, and how its pool treats them.

On ``-P solo`` and ``-P threads`` a job runs on until it returns, whatever the task time limit.
Once a job runs past ``CELERY_TASK_TIMEOUT`` the worker's tick raises, so the liveness file turns
stale. On ``solo`` the job runs on the hub thread, so the job's own loop ticks the worker's
progress while it runs. A job on a pool that runs it in the worker process holds the fork gate's
job span for its whole run.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from tai42_kit.fork_gate import fork_gate

from tai42_backend_celery.core import job_slots


def _watch(
    *,
    budget: float | None = 60.0,
    ticks_in_job: bool = False,
    tick: Callable[[], None] = lambda: None,
    pool_name: str = "solo",
    in_process: bool = True,
) -> None:
    job_slots.watch(pool_name=pool_name, in_process=in_process, budget=budget, ticks_in_job=ticks_in_job, tick=tick)


async def _sleep_then(seconds: float, value: object = None) -> object:
    await asyncio.sleep(seconds)
    return value


def test_with_no_watch_a_job_runs_unchanged() -> None:
    assert asyncio.run(job_slots.in_job(_sleep_then(0, 7), label="tool t1")) == 7
    job_slots.check_wedged()


def test_a_job_past_the_budget_wedges_the_solo_worker() -> None:
    _watch(budget=0.05)

    async def _job() -> None:
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError) as raised:
            job_slots.check_wedged()
        assert str(raised.value) == (
            "a job slot of this worker holds a job past CELERY_TASK_TIMEOUT=0.05s on the solo pool, which "
            "cannot end it; reporting stops so the probe ends this worker: tool t1"
        )

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))

    job_slots.check_wedged()


def test_a_job_within_the_budget_wedges_nothing() -> None:
    _watch(budget=60.0)

    async def _job() -> None:
        job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))


def test_on_threads_one_overdue_job_wedges_the_worker() -> None:
    _watch(budget=0.05, pool_name="threads")

    async def _overdue() -> None:
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match=r"on the threads pool, .*: tool a$"):
            job_slots.check_wedged()

    async def _main() -> None:
        await asyncio.gather(
            job_slots.in_job(_overdue(), label="tool a"),
            job_slots.in_job(_sleep_then(0.0), label="tool b"),
        )

    asyncio.run(_main())


def test_on_threads_every_overdue_job_is_named() -> None:
    _watch(budget=0.05, pool_name="threads")

    async def _both() -> None:
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match=r"on the threads pool, .*: tool a, tool b$"):
            job_slots.check_wedged()

    async def _main() -> None:
        await asyncio.gather(
            job_slots.in_job(_both(), label="tool a"),
            job_slots.in_job(_sleep_then(0.15), label="tool b"),
        )

    asyncio.run(_main())


@pytest.mark.parametrize("pool_name", ["prefork", "gevent", "eventlet"])
def test_a_pool_that_ends_its_jobs_wedges_nothing(pool_name: str) -> None:
    _watch(budget=None, pool_name=pool_name, in_process=pool_name != "prefork", ticks_in_job=True)

    async def _job() -> None:
        await asyncio.sleep(0.01)
        job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))


def test_a_job_in_another_process_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    _watch(budget=0.0)
    monkeypatch.setattr(job_slots.os, "getpid", lambda: -1)

    async def _job() -> None:
        await asyncio.sleep(0.01)
        job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))


def test_the_job_s_exception_propagates_and_frees_its_slot() -> None:
    _watch(budget=0.0, ticks_in_job=True, tick=lambda: None)

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

        _watch(ticks_in_job=True, tick=_tick)

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
    _watch(ticks_in_job=False, tick=lambda: ticks.append("tick"), pool_name="threads")

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

    _watch(ticks_in_job=True, tick=_tick)

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
    _watch(budget=0.0, ticks_in_job=True, tick=lambda: ticks.append("tick"))

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
    _watch(budget=0.05)

    async def _job() -> None:
        _watch(budget=0.05)
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match="a job slot of this worker"):
            job_slots.check_wedged()

    asyncio.run(job_slots.in_job(_job(), label="tool t1"))


@pytest.fixture
def held_rebuild() -> Iterator[threading.Event]:
    """Hold the process's fork gate as a config reload's re-import does, until the yielded event is set."""
    held, release = threading.Event(), threading.Event()

    def _rebuild() -> None:
        with fork_gate.exclusive(timeout=5.0):
            held.set()
            release.wait(10.0)

    thread = threading.Thread(target=_rebuild, daemon=True)
    thread.start()
    assert held.wait(5.0)
    yield release
    release.set()
    thread.join(10.0)


def test_an_in_process_job_holds_the_fork_gate_for_its_whole_run() -> None:
    _watch(pool_name="threads")

    with job_slots.in_process_job():
        assert fork_gate.live_spans == 1

    assert fork_gate.live_spans == 0


def test_an_in_process_job_waits_out_a_re_import_and_ticks_meanwhile(
    monkeypatch: pytest.MonkeyPatch, held_rebuild: threading.Event
) -> None:
    monkeypatch.setattr(job_slots, "_TICK_SECONDS", 0.01)
    ticks: list[float] = []
    released_at: list[float] = []

    def tick() -> None:
        ticks.append(time.monotonic())
        if len(ticks) == 2:
            released_at.append(time.monotonic())
            held_rebuild.set()

    _watch(pool_name="solo", tick=tick)

    with job_slots.in_process_job():
        entered = time.monotonic()

    assert released_at, "the job entered before the re-import ended"
    assert entered >= released_at[0]
    assert all(at < entered for at in ticks)


def test_a_job_in_a_pool_child_takes_no_span(monkeypatch: pytest.MonkeyPatch, held_rebuild: threading.Event) -> None:
    _watch(pool_name="solo")
    monkeypatch.setattr(job_slots.os, "getpid", lambda: -1)

    with job_slots.in_process_job():
        assert fork_gate.live_spans == 0


def test_a_prefork_pool_takes_no_span(held_rebuild: threading.Event) -> None:
    _watch(pool_name="prefork", in_process=False, budget=None)

    with job_slots.in_process_job():
        assert fork_gate.live_spans == 0


def test_with_no_watch_a_job_takes_no_span(held_rebuild: threading.Event) -> None:
    with job_slots.in_process_job():
        assert fork_gate.live_spans == 0
