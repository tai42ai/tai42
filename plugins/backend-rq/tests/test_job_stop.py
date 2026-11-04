"""A stop of a running job on the non-forking worker (the ``solo`` pool), run on the test's own threads.

A real :class:`~tai42_backend_rq.worker_pools.CustomRQSimpleWorker` works one in-memory server's
queue on a thread of the test, which stops it at the end; the test hands the worker rq's stop-job
command the way rq's pub/sub listener does (``handle_stop_job_command``) once the job has marked
its start.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time
from collections.abc import Callable, Iterator
from types import CodeType, FrameType
from typing import Any

import pytest
from rq import Queue, Retry, get_current_job
from rq.command import handle_stop_job_command
from rq.job import Job, JobStatus
from rq.timeouts import BaseDeathPenalty
from tai42_kit.settings import reset_all_settings

from tai42_backend_rq import worker_pools
from tai42_backend_rq.death_penalties import WatchedDeathPenalty, WatchedTimerDeathPenalty
from tai42_backend_rq.settings import rq_settings


def _mark(step: str) -> None:
    job = get_current_job()
    assert job is not None
    job.connection.rpush(f"marks:{job.id}", step)


async def waits(seconds: float) -> None:
    """An async job: mark its start, await ``seconds``, mark its cleanup however it ends."""
    _mark("started")
    try:
        await asyncio.sleep(seconds)
    finally:
        _mark("cleanup")


def blocks(seconds: float) -> None:
    """A sync job blocked in C code: mark its start, sleep ``seconds`` in one call, mark its end."""
    _mark("started")
    time.sleep(seconds)
    _mark("after")


async def catches_its_budget_and_runs_on_until_let_go() -> None:
    """An async job that catches the cancel its budget makes and runs on until the test lets it go."""
    _mark("started")
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        _mark("budget")
    job = get_current_job()
    assert job is not None
    while not job.connection.exists(f"let-go:{job.id}"):
        await asyncio.sleep(0.02)
    _mark("ran on")


# What the job ``lands_a_raise_at_its_section_end`` does as its guarded section starts to end: the code of
# the end step whose entry it waits for, and what it then runs on a thread it joins.
_AT_THE_SECTION_END: list[tuple[CodeType, Callable[[], None]]] = []


def lands_a_raise_at_its_section_end() -> None:
    """A sync job whose thread, as its guarded section starts to end, runs what the test put in ``_AT_THE_SECTION_END``.

    That raises into the job's thread; the raise lands as the named end step is entered, before it runs.
    """
    end_step, action = _AT_THE_SECTION_END.pop()

    def at_the_section_end(frame: FrameType, event: str, arg: Any) -> None:
        if event == "call" and frame.f_code is end_step:
            sys.settrace(None)
            acting = threading.Thread(target=action)
            acting.start()
            acting.join()

    sys.settrace(at_the_section_end)
    _mark("started")


def fails_on_its_second_run() -> int:
    """A job that counts its runs under its id and fails on the second one."""
    job = get_current_job()
    assert job is not None
    runs = job.connection.incr(f"runs:{job.id}")
    assert isinstance(runs, int)
    if runs == 2:
        raise RuntimeError("the second run fails")
    return runs


def _marks(conn: Any, job: Job) -> list[str]:
    return [m.decode() for m in conn.lrange(f"marks:{job.id}", 0, -1)]


def _until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture
def worker(rq_server, monkeypatch) -> Iterator[worker_pools.CustomRQSimpleWorker]:
    """A solo worker working the in-memory server's queue on a thread, stopped and joined at the end."""
    monkeypatch.setattr(worker_pools, "_DEQUEUE_POLL_SECONDS", 0.1)
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    working = worker_pools.CustomRQSimpleWorker(
        [queue], connection=rq_server, report_progress=lambda *, next_within: None
    )
    thread = threading.Thread(target=working.work, daemon=True)
    thread.start()
    yield working
    working._stop_requested = True
    thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.fixture
def one_second_budget() -> Iterator[None]:
    """``RQ_TASK_TIMEOUT`` of one second for the test's jobs."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("RQ_TASK_TIMEOUT", "1")
        reset_all_settings()
        yield
    reset_all_settings()


def _stop(worker: worker_pools.CustomRQSimpleWorker, job: Job) -> None:
    """What rq's pub/sub listener does on the stop-job command for ``job``."""
    handle_stop_job_command(worker, {"command": "stop-job", "job_id": job.id})


def _interrupts_recorded(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record whether each stop the worker makes reaches its job's section (:meth:`WatchedDeathPenalty.interrupt`)."""
    reached: list[bool] = []
    interrupt = WatchedDeathPenalty.interrupt

    def recorded(job_id: str) -> bool:
        reached.append(interrupt(job_id))
        return reached[-1]

    monkeypatch.setattr(WatchedDeathPenalty, "interrupt", recorded)
    return reached


def _stop_as_the_execution_ends(
    monkeypatch: pytest.MonkeyPatch, worker: worker_pools.CustomRQSimpleWorker
) -> list[str]:
    """Send the stop of the worker's first job as rq ends that job's execution, on its success or failure path.

    The stop is sent from a thread of its own, as rq's stop-job listener sends it, which the job's thread joins.
    """
    handle_execution_ended = worker.handle_execution_ended
    stops_sent: list[str] = []

    def stop_once(job: Job, *args: Any, **kwargs: Any) -> Any:
        if not stops_sent:
            stops_sent.append(job.id)
            stopping = threading.Thread(target=_stop, args=(worker, job))
            stopping.start()
            stopping.join()
        return handle_execution_ended(job, *args, **kwargs)

    monkeypatch.setattr(worker, "handle_execution_ended", stop_once)
    return stops_sent


def test_a_stop_ends_a_running_job_and_the_worker_runs_the_next(rq_server, worker):
    """The stop is raised into the job's thread as its time limit is: the job's async work is
    cancelled and run to its end, rq marks the job stopped, and the worker goes on."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job = queue.enqueue(waits, 30.0)
    following = queue.enqueue("os.getpid")
    _until(lambda: _marks(rq_server, job) == ["started"])

    _stop(worker, job)
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)

    assert job.get_status(refresh=True) == JobStatus.STOPPED
    assert _marks(rq_server, job) == ["started", "cleanup"]
    result = job.latest_result()
    assert result is not None
    assert result.exc_string is not None
    assert f"JobStopped: job {job.id} stopped by RQ's stop-job command" in result.exc_string
    assert "JobTimeoutException" not in result.exc_string


def test_a_job_blocked_in_c_code_is_stopped_once_the_call_returns(rq_server, worker):
    """The stop lands at the job's next Python instruction, so a job blocked in one C call is
    ended when the call returns, not before, as the pool's time limit is."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job = queue.enqueue(blocks, 1.5)
    _until(lambda: _marks(rq_server, job) == ["started"])
    stopped_at = time.monotonic()

    _stop(worker, job)
    _until(lambda: job.get_status(refresh=True) != JobStatus.STARTED)

    assert job.get_status(refresh=True) == JobStatus.STOPPED
    assert _marks(rq_server, job) == ["started"]
    assert time.monotonic() - stopped_at > 1.0


def test_a_stop_that_arrives_once_the_job_has_ended_leaves_the_worker_alone(rq_server, worker):
    """A stop whose job has ended finds no guarded section: it raises nothing, and the worker's next job runs."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    ended = queue.enqueue("os.getpid")
    _until(lambda: ended.get_status(refresh=True) == JobStatus.FINISHED)
    worker._stopped_job_id = ended.id

    worker.kill_horse()

    following = queue.enqueue("os.getpid")
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)
    assert worker._stopped_job_id is None


@pytest.mark.usefixtures("one_second_budget")
def test_a_job_awaiting_past_its_budget_is_cancelled_at_the_budget(rq_server, worker):
    """The budget's timeout is raised into the job's thread and wakes the loop its coroutine waits on, so the
    coroutine is cancelled and its cleanup run at the budget, not when its wait would have ended."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job = queue.enqueue(waits, 30.0)
    _until(lambda: _marks(rq_server, job) == ["started"])
    started = time.monotonic()

    _until(lambda: job.get_status(refresh=True) != JobStatus.STARTED)

    assert time.monotonic() - started < 1 + 2
    assert job.get_status(refresh=True) == JobStatus.FAILED
    assert _marks(rq_server, job) == ["started", "cleanup"]
    result = job.latest_result()
    assert result is not None
    assert result.exc_string is not None
    assert "JobTimeoutException: Task exceeded maximum timeout value (1 seconds)" in result.exc_string


@pytest.mark.usefixtures("one_second_budget")
def test_a_job_that_caught_its_budget_s_cancel_takes_no_stop_and_is_marked_stopped_as_it_fails(
    rq_server, worker, monkeypatch
):
    """The budget's raise closes the job's section, so a stop sent once the job's async work has caught
    the cancel raises nothing into the job, and its deadline stays recorded. The stop's mark stays: the
    job fails on its budget once its work returns, and rq marks it stopped."""
    reached = _interrupts_recorded(monkeypatch)
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job = queue.enqueue(catches_its_budget_and_runs_on_until_let_go)
    following = queue.enqueue("os.getpid")
    _until(lambda: _marks(rq_server, job) == ["started", "budget"])

    _stop(worker, job)
    overdue = WatchedDeathPenalty.overdue(job.id, margin=0)
    rq_server.set(f"let-go:{job.id}", 1)
    _until(lambda: job.get_status(refresh=True) != JobStatus.STARTED)

    assert reached == [False]
    assert overdue == 1
    assert job.get_status(refresh=True) == JobStatus.STOPPED
    assert _marks(rq_server, job) == ["started", "budget", "ran on"]
    result = job.latest_result()
    assert result is not None
    assert result.exc_string is not None
    assert "JobTimeoutException: Task exceeded maximum timeout value (1 seconds)" in result.exc_string
    assert "JobStopped" not in result.exc_string
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)


def _budget_fired_on_demand(monkeypatch: pytest.MonkeyPatch, job_id: str) -> Callable[[], None]:
    """Make the budget of ``job_id``'s section fire when the returned call asks, rather than at the budget.

    The call fires it and returns once the timeout has been raised into the job's thread.
    """
    fire = threading.Event()
    timers: list[threading.Timer] = []
    new_timer = WatchedTimerDeathPenalty.new_timer

    def fires_on_demand(penalty: WatchedTimerDeathPenalty) -> threading.Timer:
        if penalty._job_id != job_id:
            return new_timer(penalty)

        def fire_when_asked() -> None:
            fire.wait(timeout=10)
            penalty.handle_death_penalty()

        timers.append(threading.Timer(0, fire_when_asked))
        return timers[-1]

    def fire_now() -> None:
        fire.set()
        timers[-1].join(timeout=10)

    monkeypatch.setattr(WatchedTimerDeathPenalty, "new_timer", fires_on_demand)
    return fire_now


@pytest.mark.parametrize("landing", ["stop", "budget"])
def test_a_raise_that_skips_the_section_s_end_leaves_no_record_once_the_job_s_run_ends(
    rq_server, worker, monkeypatch, landing
):
    """A stop or the budget's timeout lands as the section's end is entered and skips it; rq takes it as
    a late timeout and the job finishes. Its run's end drops what the skipped end left: the section's
    deadline, its stoppable record and the mark of the stop."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job_id = f"skips-its-end-{landing}"
    end_step = WatchedDeathPenalty.cancel_death_penalty.__code__
    if landing == "stop":
        _AT_THE_SECTION_END.append((end_step, lambda: _stop(worker, Job(job_id, connection=rq_server))))
    else:
        _AT_THE_SECTION_END.append((end_step, _budget_fired_on_demand(monkeypatch, job_id)))
    job = queue.enqueue(lands_a_raise_at_its_section_end, job_id=job_id)

    _until(lambda: job.get_status(refresh=True) == JobStatus.FINISHED)
    following = queue.enqueue("os.getpid")
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)

    assert job_id not in WatchedDeathPenalty._armed
    assert job_id not in WatchedDeathPenalty._stoppable
    assert worker._stopped_job_id is None
    assert WatchedDeathPenalty.interrupt(job_id) is False


class _ElsewhereRaisedError(RuntimeError):
    """An exception raised into the job's thread by neither its budget nor a stop."""


def test_an_exception_from_elsewhere_that_skips_the_section_s_end_leaves_no_stoppable_section_once_the_run_ends(
    rq_server, worker
):
    """An exception the penalty did not raise lands as the section's end is entered, skips that end with the
    section still open, and fails the job. Its run's end closes the section and drops its records."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job_id = "skips-its-end-from-elsewhere"

    def raise_from_elsewhere() -> None:
        penalty = WatchedDeathPenalty._stoppable[job_id]
        assert isinstance(penalty, WatchedTimerDeathPenalty)
        thread_id = penalty._target_thread_id
        assert thread_id is not None
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), ctypes.py_object(_ElsewhereRaisedError))

    _AT_THE_SECTION_END.append((WatchedDeathPenalty.cancel_death_penalty.__code__, raise_from_elsewhere))
    job = queue.enqueue(lands_a_raise_at_its_section_end, job_id=job_id)

    _until(lambda: job.get_status(refresh=True) == JobStatus.FAILED)
    following = queue.enqueue("os.getpid")
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)

    assert job_id not in WatchedDeathPenalty._armed
    assert job_id not in WatchedDeathPenalty._stoppable
    assert WatchedDeathPenalty.interrupt(job_id) is False


def test_a_budget_that_lands_as_rq_s_section_end_is_entered_leaves_rq_s_failure_handling_unstoppable(
    rq_server, worker, monkeypatch
):
    """The budget's timeout lands at the entry of rq's end of the job's section, before that end runs, and
    fails the job whose body completed. A stop sent while rq handles that failure raises nothing: rq marks
    the job stopped, the worker runs its next job, and no record of the job is left."""
    reached = _interrupts_recorded(monkeypatch)
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job_id = "budget-at-rq-s-section-end"
    _AT_THE_SECTION_END.append((BaseDeathPenalty.__exit__.__code__, _budget_fired_on_demand(monkeypatch, job_id)))
    stops_sent = _stop_as_the_execution_ends(monkeypatch, worker)
    job = queue.enqueue(lands_a_raise_at_its_section_end, job_id=job_id)

    _until(lambda: job.get_status(refresh=True) not in (JobStatus.QUEUED, JobStatus.STARTED))
    following = queue.enqueue("os.getpid")
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)

    assert stops_sent == [job_id]
    assert reached == [False]
    assert job.get_status(refresh=True) == JobStatus.STOPPED
    result = job.latest_result()
    assert result is not None
    assert result.exc_string is not None
    assert "JobTimeoutException" in result.exc_string
    assert "JobStopped" not in result.exc_string
    assert job_id not in WatchedDeathPenalty._armed
    assert job_id not in WatchedDeathPenalty._stoppable
    assert worker._stopped_job_id is None


def test_a_budget_that_lands_inside_the_section_s_end_leaves_rq_s_success_handling_unstoppable(
    rq_server, worker, monkeypatch
):
    """The budget's timeout lands at the entry of the penalty's end of the job's section, which rq takes
    as that end. A stop sent while rq handles the job's success raises nothing: the job finishes, and
    neither the stop's mark nor any record of the job is left."""
    reached = _interrupts_recorded(monkeypatch)
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job_id = "budget-in-the-section-s-end"
    _AT_THE_SECTION_END.append(
        (WatchedDeathPenalty.cancel_death_penalty.__code__, _budget_fired_on_demand(monkeypatch, job_id))
    )
    stops_sent = _stop_as_the_execution_ends(monkeypatch, worker)
    job = queue.enqueue(lands_a_raise_at_its_section_end, job_id=job_id)

    _until(lambda: job.get_status(refresh=True) not in (JobStatus.QUEUED, JobStatus.STARTED))
    following = queue.enqueue("os.getpid")
    _until(lambda: following.get_status(refresh=True) == JobStatus.FINISHED)

    assert stops_sent == [job_id]
    assert job.get_status(refresh=True) == JobStatus.FINISHED
    assert reached == [False]
    assert job_id not in WatchedDeathPenalty._armed
    assert job_id not in WatchedDeathPenalty._stoppable
    assert worker._stopped_job_id is None


def test_a_stop_that_reaches_no_running_section_leaves_no_mark_on_the_job_s_next_run(rq_server, worker, monkeypatch):
    """A stop that arrives once the job's section has ended raises nothing, and leaves no mark behind:
    the same job's next run fails, is retried as its retry asks, and finishes."""
    queue = Queue(rq_settings().queue_name, connection=rq_server)
    job_id = "runs-again"
    stops_sent = _stop_as_the_execution_ends(monkeypatch, worker)
    first = queue.enqueue(fails_on_its_second_run, job_id=job_id, retry=Retry(max=1))
    _until(lambda: first.get_status(refresh=True) == JobStatus.FINISHED)

    again = queue.enqueue(fails_on_its_second_run, job_id=job_id, retry=Retry(max=1))
    _until(lambda: again.get_status(refresh=True) in (JobStatus.FINISHED, JobStatus.STOPPED, JobStatus.FAILED))

    assert stops_sent == [job_id]
    assert again.get_status(refresh=True) == JobStatus.FINISHED
    assert rq_server.get(f"runs:{job_id}") == b"3"
