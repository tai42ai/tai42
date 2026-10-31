"""A wait for another job's result refuses inside a backend job and runs outside one."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tai42_kit.backend.job_context import assert_not_in_job, in_job, running_job

_REFUSAL = "the_wait cannot wait for another job's result from inside a job: chain it with a callback instead"


def test_a_wait_outside_a_job_passes():
    assert_not_in_job("the_wait")


def test_a_wait_inside_a_job_raises_naming_the_waiter():
    with running_job(), pytest.raises(RuntimeError) as refused:
        assert_not_in_job("the_wait")
    assert str(refused.value) == _REFUSAL


def test_the_job_mark_ends_with_the_job():
    with running_job():
        pass
    assert_not_in_job("the_wait")


def test_the_job_mark_ends_when_the_job_raises():
    with pytest.raises(ValueError, match="job failed"), running_job():
        raise ValueError("job failed")
    assert_not_in_job("the_wait")


async def test_a_coroutine_and_a_worker_thread_of_the_job_carry_its_mark():
    """The mark rides the job's context: a task the job awaits and a sync step it hands to
    ``asyncio.to_thread`` both run inside the job."""
    with running_job(), pytest.raises(RuntimeError, match="from inside a job"):
        await asyncio.create_task(asyncio.to_thread(assert_not_in_job, "the_wait"))


def test_a_thread_started_outside_the_job_is_not_in_it():
    """Another job slot's thread does not see this job's mark."""
    failures: list[BaseException] = []

    def other_slot() -> None:
        try:
            assert_not_in_job("the_wait")
        except BaseException as exc:
            failures.append(exc)

    with running_job():
        slot = threading.Thread(target=other_slot)
        slot.start()
        slot.join()
    assert failures == []


def test_in_job_reads_the_mark_inside_the_job_only():
    assert not in_job()
    with running_job():
        assert in_job()
    assert not in_job()


def test_in_job_is_false_on_a_thread_started_without_the_jobs_context():
    seen: list[bool] = []
    with running_job():
        slot = threading.Thread(target=lambda: seen.append(in_job()))
        slot.start()
        slot.join()
    assert seen == [False]
