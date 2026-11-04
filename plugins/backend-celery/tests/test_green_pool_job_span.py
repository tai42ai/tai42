"""A job on a green pool holds the fork gate's job span, and a re-import tells its own job from the others.

A green pool (``-P gevent``, ``-P eventlet``) patches the process and runs each job on a green
thread of the pool, in the worker process itself, so every job holds the fork gate's job span
(:func:`job_slots.in_process_job`). A green thread starts from an empty context: a job's span is
marked in the context of the job's own green thread, and a re-import the job drives itself
(``fork_gate.exclusive_async`` awaited from the job's coroutine, as the reload gate holds the
fork gate around a re-importing body) carries that mark, so it waits for every other job's span
but never for its own. A re-import started during the job from a green thread of its own, run
on the host's loop as a reload door runs it, carries no mark, so it waits for the job's span as
for any other. A job the pool ends (its time limit, a revoke that terminates it) holds its
span until its cancelled coroutine has ended, its cleanup included, even when the coroutine
swallows the cancel; the job then ends with the pool's exception, and an error the coroutine's
cleanup raised is logged with its traceback, while an exit it raised ends the host's run of its
loop, which the jobs run on. A further interrupt of that wait
is forwarded to the coroutine as another cancel, and the job still holds its span until the
coroutine has ended. A coroutine that still runs a tick past the budget is a wedged job.

Each case runs on the pool's real celery ``TaskPool`` in a fresh patched process
(:mod:`_green_pool_process`).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from ._green_pool_process import GREEN_POOLS, run_on_green_pool

# A re-import that waited for its own job's span would wait out ``QUIESCE`` and log the ERROR the
# fork gate reports for it.
_CASES = """
import asyncio
import logging
import time

from tai42_kit.fork_gate import fork_gate

from tai42_backend_celery.core import job_slots

QUIESCE = 5.0
HOLD = 0.3


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []
        self.loop_tracebacks = []

    def emit(self, record):
        self.messages.append(record.getMessage())
        if record.name == "tai42_kit.backend.shared_loop" and record.exc_info:
            self.loop_tracebacks.append(logging.Formatter().formatException(record.exc_info))


errors = _Errors()
logging.getLogger().addHandler(errors)
stamps = {}
failures = []


async def rebuild():
    async with fork_gate.exclusive_async(timeout=QUIESCE):
        stamps["rebuild_entered"] = time.monotonic()
        stamps["spans_during_rebuild"] = fork_gate.live_spans


def rebuild_on_the_host():
    asyncio.run_coroutine_threadsafe(rebuild(), HOST_LOOP).result(BOUND)


def recorded(job):
    def run():
        try:
            job()
        except BaseException as exc:
            failures.append(repr(exc))

    return run


def own_reload():
    other_held, release_other = Event(), Event()

    def other_job():
        with job_slots.in_process_job(celery_app):
            other_held.set()
            release_other.wait(BOUND)

    def release():
        stamps["other_released"] = time.monotonic()
        release_other.set()

    async def reload_from_the_job():
        stamps["spans_before_rebuild"] = fork_gate.live_spans
        spawn_later(HOLD, release)
        await rebuild()

    def reloading_job():
        as_task("reloading", reload_from_the_job())

    pool = start_pool(2)
    pool.apply_async(recorded(other_job), callback=lambda _: None)
    assert other_held.wait(BOUND), "the other job never held its span"
    pool.apply_async(recorded(reloading_job), callback=lambda _: None)
    stop_pool(pool)


def reload_during_the_job():
    rebuilt = Event()

    def rebuild_on_its_own():
        rebuild_on_the_host()
        rebuilt.set()

    async def job_body():
        spawn(recorded(rebuild_on_its_own))
        await asyncio.sleep(HOLD)
        stamps["job_body_ended"] = time.monotonic()
        stamps["entered_during_job"] = "rebuild_entered" in stamps

    def job():
        as_task("running", job_body())

    pool = start_pool(2)
    pool.apply_async(recorded(job), callback=lambda _: None)
    stop_pool(pool)
    assert rebuilt.wait(BOUND), "the re-import never ran"


def ended_by_the_pool():
    how, on_cancel = CASE_ARGS[1:3]
    waiting, rebuilt, coroutine_ended = Event(), Event(), Event()
    pids = {}

    def rebuild_on_its_own():
        rebuild_on_the_host()
        rebuilt.set()

    async def cleans_up_when_cancelled():
        waiting.set()
        try:
            await asyncio.sleep(BOUND)
        except asyncio.CancelledError:
            # The pool ended the job: a re-import starts now, while the coroutine cleans up.
            stamps["spans_at_cancel"] = fork_gate.live_spans
            spawn(recorded(rebuild_on_its_own))
            await asyncio.sleep(HOLD)
            stamps["spans_during_cleanup"] = fork_gate.live_spans
            stamps["coroutine_ended"] = time.monotonic()
            coroutine_ended.set()
            if on_cancel == "reraises":
                raise
            if on_cancel == "raises_in_cleanup":
                raise OSError("cleanup failed") from None
            if on_cancel == "exits_in_cleanup":
                raise SystemExit(3) from None
        return "kept going"

    def job():
        pids["job"] = pool.getpid()
        try:
            stamps["returned"] = as_task("ended", cleans_up_when_cancelled())
        except BaseException as exc:
            stamps["raised"] = type(exc).__name__
            raise

    pool = start_pool(1)
    if how == "time_limit":
        pool.apply_async(job, callback=lambda _: None, timeout=HOLD, timeout_callback=lambda *_: None)
    else:
        pool.apply_async(job, callback=lambda _: None)
        assert waiting.wait(BOUND), "the job's coroutine never started"
        pool.terminate_job(pids["job"])
    stop_pool(pool)
    assert coroutine_ended.wait(BOUND), "the job's coroutine never ended"
    assert rebuilt.wait(BOUND), "the re-import never ran"


def wedged():
    try:
        job_slots.check_wedged(celery_app)
    except RuntimeError as exc:
        return str(exc)
    return None


def runs_on_after_the_cancel():
    cancelled, job_ended = Event(), Event()
    released = []

    async def runs_on():
        try:
            await asyncio.sleep(BOUND)
        except asyncio.CancelledError:
            stamps["wedged_at_cancel"] = wedged()
            cancelled.set()
            while not released:
                await asyncio.sleep(0.01)

    def job():
        try:
            as_task("held", runs_on())
        except BaseException as exc:
            stamps["raised"] = type(exc).__name__
            raise
        finally:
            job_ended.set()

    pool = start_pool(1, task_timeout=HOLD)
    pool.apply_async(job, callback=lambda _: None, timeout=HOLD, timeout_callback=lambda *_: None)
    assert cancelled.wait(BOUND), "the job's coroutine was never cancelled"
    Event().wait(job_slots.PROGRESS_TICK_SECONDS + 0.5)
    stamps["wedged_past_the_tick"] = wedged()
    stamps["spans_while_running_on"] = fork_gate.live_spans
    stamps["job_ended_while_running_on"] = job_ended.is_set()
    released.append(True)
    assert job_ended.wait(BOUND), "the job never ended once its coroutine returned"
    stop_pool(pool)
    stamps["wedged_after"] = wedged()


def interrupted_again_while_unwinding():
    first_cancel, rebuilt, coroutine_ended = Event(), Event(), Event()
    cancels = []
    pids = {}

    def rebuild_on_its_own():
        rebuild_on_the_host()
        rebuilt.set()

    async def unwinds_slowly():
        try:
            await asyncio.sleep(BOUND)
        except asyncio.CancelledError:
            cancels.append("first")
            first_cancel.set()
            try:
                await asyncio.sleep(BOUND / 4)
            except asyncio.CancelledError:
                cancels.append("second")
                stamps["spans_at_second_cancel"] = fork_gate.live_spans
                spawn(recorded(rebuild_on_its_own))
                await asyncio.sleep(HOLD)
                stamps["spans_during_cleanup"] = fork_gate.live_spans
                raise
            finally:
                stamps["coroutine_ended"] = time.monotonic()
                coroutine_ended.set()
            raise

    def job():
        pids["job"] = pool.getpid()
        try:
            as_task("unwinding", unwinds_slowly())
        except BaseException as exc:
            stamps["job_ended"] = time.monotonic()
            stamps["raised"] = type(exc).__name__
            stamps["context"] = type(exc.__context__).__name__ if exc.__context__ is not None else None
            raise

    pool = start_pool(1)
    pool.apply_async(job, callback=lambda _: None, timeout=HOLD, timeout_callback=lambda *_: None)
    assert first_cancel.wait(BOUND), "the job's coroutine was never cancelled"
    pool.terminate_job(pids["job"])
    stop_pool(pool)
    assert coroutine_ended.wait(BOUND), "the job's coroutine never ended"
    rebuilt.wait(BOUND)
    stamps["cancels"] = cancels


def case():
    {
        "own_reload": own_reload,
        "reload_during_the_job": reload_during_the_job,
        "ended_by_the_pool": ended_by_the_pool,
        "runs_on_after_the_cancel": runs_on_after_the_cancel,
        "interrupted_again_while_unwinding": interrupted_again_while_unwinding,
    }[CASE_ARGS[0]]()
    return {
        "stamps": stamps,
        "failures": failures,
        "errors": errors.messages,
        "loop_tracebacks": errors.loop_tracebacks,
        "spans_after": fork_gate.live_spans,
    }


report(on_the_host(case))
"""


def _run_case(pool_name: str, case: str, *case_args: str) -> dict[str, Any]:
    return run_on_green_pool(pool_name, _CASES, case, *case_args)


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_a_re_import_a_job_drives_waits_for_another_job_s_span_but_not_its_own(pool_name: str) -> None:
    outcome = _run_case(pool_name, "own_reload")

    assert outcome["failures"] == []
    stamps = outcome["stamps"]
    assert stamps["spans_before_rebuild"] == 2, "the two jobs on their green threads do not hold a span each"
    assert stamps["rebuild_entered"] >= stamps["other_released"], "the re-import did not wait for the other job"
    assert stamps["rebuild_entered"] - stamps["other_released"] < 1.0, "the re-import waited for its own job"
    assert stamps["spans_during_rebuild"] == 1, "the job's own span did not stay open around its re-import"
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0


@pytest.mark.parametrize("pool_name", GREEN_POOLS)
def test_a_re_import_on_a_green_thread_of_its_own_waits_for_the_running_job(pool_name: str) -> None:
    outcome = _run_case(pool_name, "reload_during_the_job")

    assert outcome["failures"] == []
    stamps = outcome["stamps"]
    assert stamps["entered_during_job"] is False, "the re-import ran while the job held its span"
    assert stamps["rebuild_entered"] >= stamps["job_body_ended"]
    assert stamps["spans_during_rebuild"] == 0
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0


_ENDED_BY_THE_POOL = pytest.mark.parametrize(
    ("pool_name", "how", "raised"),
    [
        pytest.param("gevent", "time_limit", "Timeout", id="gevent-time_limit"),
        pytest.param("gevent", "revoke", "GreenletExit", id="gevent-revoke"),
        pytest.param("eventlet", "revoke", "GreenletExit", id="eventlet-revoke"),
    ],
)


@_ENDED_BY_THE_POOL
@pytest.mark.parametrize("on_cancel", ["reraises", "swallows"])
def test_a_job_the_pool_ends_holds_its_span_until_its_coroutine_has_ended(
    pool_name: str, how: str, raised: str, on_cancel: str
) -> None:
    """The eventlet pool takes no time limit, so only a revoke ends a job there."""
    outcome = _run_case(pool_name, "ended_by_the_pool", how, on_cancel)

    assert outcome["failures"] == []
    stamps = outcome["stamps"]
    assert stamps["raised"] == raised, "the pool's exception did not go up the job"
    assert "returned" not in stamps
    assert (stamps["spans_at_cancel"], stamps["spans_during_cleanup"]) == (1, 1), (
        "the job left its span while its coroutine still ran"
    )
    assert stamps["rebuild_entered"] >= stamps["coroutine_ended"], "the re-import ran inside the job's coroutine"
    assert stamps["spans_during_rebuild"] == 0
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0


@_ENDED_BY_THE_POOL
def test_an_error_the_coroutine_s_cleanup_raises_after_the_pool_ended_the_job_is_logged(
    pool_name: str, how: str, raised: str
) -> None:
    """The job still ends with the pool's exception, which celery reports; the cleanup's error is logged."""
    outcome = _run_case(pool_name, "ended_by_the_pool", how, "raises_in_cleanup")

    assert outcome["failures"] == []
    stamps = outcome["stamps"]
    assert stamps["raised"] == raised, "the pool's exception did not go up the job"
    assert (stamps["spans_at_cancel"], stamps["spans_during_cleanup"]) == (1, 1), (
        "the job left its span while its coroutine still ran"
    )
    assert stamps["rebuild_entered"] >= stamps["coroutine_ended"], "the re-import ran inside the job's coroutine"
    assert len(outcome["errors"]) == 1, outcome["errors"]
    assert outcome["errors"][0].startswith(
        "the job ended with this error while its wait was interrupted; the job raises "
    )
    [traceback] = outcome["loop_tracebacks"]
    assert traceback.splitlines()[-1] == "OSError: cleanup failed"
    assert "cleans_up_when_cancelled" in traceback, "the log carries no traceback into the coroutine"
    assert outcome["spans_after"] == 0


@_ENDED_BY_THE_POOL
def test_an_exit_the_coroutine_s_cleanup_raises_after_the_pool_ended_the_job_ends_the_host_s_run_of_its_loop(
    pool_name: str, how: str, raised: str
) -> None:
    """asyncio throws the exit out of the loop as well as into the task, and the loop is the host's."""
    assert _run_case(pool_name, "ended_by_the_pool", how, "exits_in_cleanup") == {"host_ended_with": "SystemExit(3)"}


def test_a_gevent_job_whose_coroutine_runs_on_a_tick_past_the_cancel_is_wedged() -> None:
    """The pool cancels the coroutine at the time limit; work still running a tick later did not end on it."""
    outcome = _run_case("gevent", "runs_on_after_the_cancel")

    assert outcome["failures"] == []
    stamps = outcome["stamps"]
    assert stamps["wedged_at_cancel"] is None, "the job was wedged before a tick had passed its budget"
    assert stamps["wedged_past_the_tick"] is not None, "a job still running past its budget is not wedged"
    assert re.fullmatch(
        r"a job slot of this worker holds a job that still runs a tick past CELERY_TASK_TIMEOUT=0\.3s on the "
        r"gevent pool, which cannot end it; reporting stops so the probe ends this worker: celery\.tool_execution held",
        stamps["wedged_past_the_tick"],
    )
    assert stamps["spans_while_running_on"] == 1, "the job left its span while its coroutine still ran"
    assert stamps["job_ended_while_running_on"] is False, "the job ended while its coroutine still ran"
    assert stamps["raised"] == "Timeout", "the pool's exception did not go up the job"
    assert stamps["wedged_after"] is None
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0


def test_a_gevent_job_interrupted_again_while_its_coroutine_unwinds_forwards_the_interrupt_and_holds_its_span() -> None:
    """A revoke after the time limit cancels the coroutine again; once it has ended, the job ends with the revoke."""
    outcome = _run_case("gevent", "interrupted_again_while_unwinding")

    assert outcome["failures"] == []
    stamps = outcome["stamps"]
    assert stamps["cancels"] == ["first", "second"], "the second interrupt did not reach the coroutine"
    assert (stamps["spans_at_second_cancel"], stamps["spans_during_cleanup"]) == (1, 1), (
        "the job left its span while its coroutine still ran"
    )
    assert stamps["job_ended"] >= stamps["coroutine_ended"], "the job ended before its coroutine"
    assert (stamps["raised"], stamps["context"]) == ("GreenletExit", "Timeout")
    assert stamps["rebuild_entered"] >= stamps["coroutine_ended"], "the re-import ran inside the job's coroutine"
    assert stamps["spans_during_rebuild"] == 0
    assert outcome["errors"] == []
    assert outcome["spans_after"] == 0
