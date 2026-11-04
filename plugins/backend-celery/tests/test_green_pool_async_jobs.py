"""Async jobs on a green pool (``-P gevent``, ``-P eventlet``) run their coroutines on the host's loop.

A green pool runs every job as a green thread of one OS thread, and asyncio runs one loop per OS
thread at a time, so every job of the worker process, whatever its task, hands its coroutine to the
loop the host runs and waits for its result. Jobs that overlap each get their own result, in the
time of one, whether they are jobs of one task or of two; a coroutine's exception reaches its job;
a ``SystemExit`` or ``KeyboardInterrupt`` raised by any task of a job's coroutine, which asyncio
throws out of the loop, ends the host's run of its loop; a job the pool ends while it waits (its
time limit, a revoke that terminates it) ends its coroutine too, and still ends with the pool's
exception; and a job whose wait is interrupted again and again raises the last interrupt, chained
to every earlier one. Once the jobs' loop is let go (``close_shared_loop``), a later job is refused
and the host's loop runs on.

Each case runs on the pool's real celery ``TaskPool`` in a fresh process shaped as the launch runs
a green pool (:mod:`_green_pool_process`).
"""

from __future__ import annotations

from typing import Any

import pytest

from ._green_pool_process import GREEN_POOLS, run_on_green_pool

# Seconds each job's coroutine sleeps; two that ran one after the other would take twice as long.
_HOLD = 0.5

_CASES = """
import asyncio
import time

from tai42_backend_celery.core.tasks import callback_task

HOLD = float(CASE_ARGS[1])
TASKS = {"tool_execution": tool_execution, "callback_task": callback_task}


def overlapping_jobs():
    outcomes = {}

    def job(n):
        try:
            outcomes[str(n)] = as_task(f"job-{n}", asyncio.sleep(HOLD, result=n))
        except Exception as exc:
            outcomes[str(n)] = repr(exc)

    pool = start_pool(2)
    started = time.monotonic()
    for n in range(2):
        pool.apply_async(job, args=(n,), callback=lambda _: None)
    stop_pool(pool)
    return {"outcomes": outcomes, "wall": time.monotonic() - started}


def two_tasks_in_turn():
    outcomes = {}
    done = {name: Event() for name in TASKS}

    def job(name, value):
        try:
            outcomes[name] = as_task(f"{name}-job", asyncio.sleep(0, result=value), task=TASKS[name])
        except BaseException as exc:
            outcomes[name] = repr(exc)
        finally:
            done[name].set()

    pool = start_pool(2)
    for value, name in enumerate(TASKS):
        pool.apply_async(job, args=(name, value), callback=lambda _: None)
        assert done[name].wait(BOUND), f"the {name} job never ended"
    stop_pool(pool)
    return {"outcomes": outcomes}


def two_tasks_at_once():
    outcomes = {}

    def job(name, value):
        try:
            outcomes[name] = as_task(f"{name}-job", asyncio.sleep(HOLD, result=value), task=TASKS[name])
        except BaseException as exc:
            outcomes[name] = repr(exc)

    pool = start_pool(2)
    started = time.monotonic()
    for value, name in enumerate(TASKS):
        pool.apply_async(job, args=(name, value), callback=lambda _: None)
    stop_pool(pool)
    return {"outcomes": outcomes, "wall": time.monotonic() - started}


def a_raising_coroutine():
    outcome = {}

    async def fails():
        raise ValueError("raised by the coroutine")

    def job():
        try:
            as_task("raising", fails())
        except ValueError as exc:
            outcome["raised"] = str(exc)

    pool = start_pool(1)
    pool.apply_async(job, callback=lambda _: None)
    stop_pool(pool)
    return outcome


EXITS = {"SystemExit": lambda: SystemExit(2), "KeyboardInterrupt": lambda: KeyboardInterrupt()}


async def raising(exc):
    raise exc


# Raise ``exc`` from the coroutine itself (``top``), from a child task made the way ``how`` names, or
# from a task the coroutine leaves running (``fire_and_forget``).
async def raising_from(how, exc):
    if how == "top":
        raise exc
    if how == "gather":
        await asyncio.gather(raising(exc))
    elif how == "create_task":
        await asyncio.create_task(raising(exc))
    elif how == "fire_and_forget":
        asyncio.ensure_future(raising(exc))
        await asyncio.sleep(BOUND)
    else:
        async with asyncio.TaskGroup() as group:
            group.create_task(raising(exc))


def an_exiting_coroutine(exit_name, how):
    pool = start_pool(1)
    pool.apply_async(lambda: as_task("exits", raising_from(how, EXITS[exit_name]())), callback=lambda _: None)
    Event().wait(BOUND)
    return {"host_ended_with": None}


# The worker's stop lets the jobs' loop go: a later job is refused, and the host's loop runs on.
def a_job_after_the_loop_was_let_go():
    outcome = {}

    def job():
        try:
            outcome["job"] = as_task("late", asyncio.sleep(0, result="ran"))
        except BaseException as exc:
            outcome["job"] = repr(exc)

    pool = start_pool(1)
    AsyncTask.close_shared_loop(celery_app)
    pool.apply_async(job, callback=lambda _: None)
    stop_pool(pool)
    outcome["host_loop_runs"] = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result=True), HOST_LOOP).result(
        BOUND
    )
    return outcome


# A job's wait interrupted three times: its own timeout, then two kills while its coroutine, which
# swallows each cancel, waits to be released. The job raises the last, chained to each earlier one.
def interrupted_three_times():
    class FirstKill(BaseException):
        pass

    class SecondKill(BaseException):
        pass

    def kill(green_thread, exc):
        if POOL == "gevent":
            green_thread.kill(exc, block=False)
        else:
            green_thread.kill(exc)

    outcome = {"cancels": 0}
    release, done = asyncio.Event(), Event()

    async def holds():
        while True:
            try:
                await release.wait()
                return "released"
            except asyncio.CancelledError:
                outcome["cancels"] += 1

    def job():
        try:
            with Timeout(HOLD):
                as_task("held", holds())
        except BaseException as exc:
            chain, link = [], exc
            while link is not None:
                chain.append(type(link).__name__)
                link = link.__context__
            outcome["chain"] = chain
        finally:
            done.set()

    start_pool(1)
    waiting = spawn(job)
    Event().wait(2 * HOLD)
    kill(waiting, FirstKill)
    Event().wait(HOLD)
    kill(waiting, SecondKill)
    Event().wait(HOLD)
    HOST_LOOP.call_soon_threadsafe(release.set)
    assert done.wait(BOUND), "the job never ended"
    return outcome


def ended_by_the_pool(end):
    outcome = {}
    waiting, cancelled = Event(), Event()
    pids = {}

    async def until_cancelled():
        waiting.set()
        try:
            await asyncio.sleep(BOUND)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        outcome["finished"] = True

    def job():
        pids["job"] = pool.getpid()
        try:
            as_task("ended", until_cancelled())
        except BaseException as exc:
            outcome["raised"] = type(exc).__name__
            raise

    pool = start_pool(1)
    end(pool, job, waiting, pids)
    stop_pool(pool)
    outcome["cancelled"] = cancelled.wait(BOUND)
    return outcome


def at_the_time_limit(pool, job, waiting, pids):
    pool.apply_async(job, callback=lambda _: None, timeout=HOLD, timeout_callback=lambda *_: None)


def by_a_revoke(pool, job, waiting, pids):
    pool.apply_async(job, callback=lambda _: None)
    assert waiting.wait(BOUND), "the job's coroutine never started"
    pool.terminate_job(pids["job"])


report(
    on_the_host(
        {
            "overlapping_jobs": overlapping_jobs,
            "two_tasks_in_turn": two_tasks_in_turn,
            "two_tasks_at_once": two_tasks_at_once,
            "a_raising_coroutine": a_raising_coroutine,
            "an_exiting_coroutine": lambda: an_exiting_coroutine(*CASE_ARGS[2:4]),
            "a_job_after_the_loop_was_let_go": a_job_after_the_loop_was_let_go,
            "interrupted_three_times": interrupted_three_times,
            "at_the_time_limit": lambda: ended_by_the_pool(at_the_time_limit),
            "by_a_revoke": lambda: ended_by_the_pool(by_a_revoke),
        }[CASE_ARGS[0]]
    )
)
"""


def _run_case(pool_name: str, case: str, *case_args: str) -> dict[str, Any]:
    return run_on_green_pool(pool_name, _CASES, case, str(_HOLD), *case_args)


_EXITS = pytest.mark.parametrize(
    ("exit_", "raised"), [("SystemExit", "SystemExit(2)"), ("KeyboardInterrupt", "KeyboardInterrupt()")]
)


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_overlapping_async_jobs_each_get_their_result_in_the_time_of_one(pool_name: str) -> None:
    outcome = _run_case(pool_name, "overlapping_jobs")

    assert outcome["outcomes"] == {"0": 0, "1": 1}
    assert outcome["wall"] < 1.8 * _HOLD, "the two jobs ran one after the other"


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_jobs_of_two_tasks_in_turn_each_get_their_result(pool_name: str) -> None:
    assert _run_case(pool_name, "two_tasks_in_turn") == {"outcomes": {"tool_execution": 0, "callback_task": 1}}


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_overlapping_jobs_of_two_tasks_each_get_their_result_in_the_time_of_one(pool_name: str) -> None:
    outcome = _run_case(pool_name, "two_tasks_at_once")

    assert outcome["outcomes"] == {"tool_execution": 0, "callback_task": 1}
    assert outcome["wall"] < 1.8 * _HOLD, "the two jobs ran one after the other"


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_a_coroutine_s_exception_reaches_its_job(pool_name: str) -> None:
    assert _run_case(pool_name, "a_raising_coroutine") == {"raised": "raised by the coroutine"}


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
@_EXITS
@pytest.mark.parametrize("how", ["top", "gather", "create_task", "TaskGroup", "fire_and_forget"])
def test_a_coroutine_s_exit_from_any_of_its_tasks_ends_the_host_s_run_of_its_loop(
    pool_name: str, exit_: str, raised: str, how: str
) -> None:
    """asyncio throws these two out of the loop as well as into the task; the loop is the host's, so its run ends."""
    assert _run_case(pool_name, "an_exiting_coroutine", exit_, how) == {"host_ended_with": raised}


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_a_job_after_the_jobs_loop_was_let_go_is_refused_and_the_host_s_loop_runs_on(pool_name: str) -> None:
    assert _run_case(pool_name, "a_job_after_the_loop_was_let_go") == {
        "job": 'RuntimeError("the jobs\' shared event loop was let go; it runs no further job")',
        "host_loop_runs": True,
    }


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_a_job_interrupted_three_times_raises_the_last_chained_to_each_earlier_one(pool_name: str) -> None:
    assert _run_case(pool_name, "interrupted_three_times") == {
        "cancels": 3,
        "chain": ["SecondKill", "FirstKill", "Timeout"],
    }


def test_a_job_ended_at_its_time_limit_ends_its_coroutine() -> None:
    """Only the gevent pool ends a job at its time limit; the eventlet pool takes none."""
    assert _run_case("gevent", "at_the_time_limit") == {"raised": "Timeout", "cancelled": True}


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_a_job_a_revoke_terminates_ends_its_coroutine(pool_name: str) -> None:
    assert _run_case(pool_name, "by_a_revoke") == {"raised": "GreenletExit", "cancelled": True}
