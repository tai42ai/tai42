"""The arq worker inspection: arq's public ``Job`` API and its queue and result queries.

arq keeps no per-worker record: its workers share the queues and leave no registry of their
names, task names, stats or queues, and a running job is not attributed to a worker. So no view
narrows to a worker, and ``ping``, ``active_workers``, ``registered_tasks``, ``worker_stats`` and
``worker_queues`` keep the base's refusal. A cancel is arq's abort mark: with ``terminate`` it
stops a running job; without it, a job is marked only while no worker has claimed it and is
refused once running, since arq has no cancel that leaves a running job alone.
"""

from __future__ import annotations

import asyncio
from typing import Any

from arq.constants import abort_jobs_ss, in_progress_key_prefix
from arq.jobs import Job, JobStatus, ResultNotFound
from arq.utils import timestamp_ms
from tai42_kit.backend import BackendInspection
from tai42_kit.backend.job_context import in_job

from tai42_backend_arq import pool
from tai42_backend_arq.jobs import holding_queue, in_progress_handle, job_handle, known_queues
from tai42_backend_arq.scheduler import wait_job_result
from tai42_backend_arq.settings import TaskFailedError, arq_settings


def _failure_detail(result: Any) -> str:
    """Human-readable detail of a stored failure result.

    The revived failure's ``repr``, or the ``repr`` of whatever else was stored.
    """
    if isinstance(result, TaskFailedError):
        return result.error_repr
    if isinstance(result, asyncio.CancelledError):
        return str(result) or repr(result)
    return repr(result)


async def _mark_abort(redis: Any, job_id: str, queue: str, *, only_if_not_started: bool) -> bool:
    """Write arq's abort mark for ``job_id`` held on ``queue``; ``False`` when it is refused.

    One transaction watching arq's in-progress key, which a worker writes in its own watched
    transaction when it claims the job. With ``only_if_not_started`` a claimed job is refused, and
    a claim landing meanwhile fails the transaction, whose retry then sees the claim, so a job that
    starts meanwhile is never marked. A deferred job is re-scored due, as arq's own abort does, so
    a worker picks it up and honors the mark now rather than at its due time.
    """
    in_progress_key = in_progress_key_prefix + job_id

    async def _mark(pipe: Any) -> bool:
        if only_if_not_started and await pipe.exists(in_progress_key):
            return False
        score = await pipe.zscore(queue, job_id)
        pipe.multi()
        if score is not None and score > timestamp_ms():
            pipe.zadd(queue, {job_id: 1}, xx=True)
        pipe.zadd(abort_jobs_ss, {job_id: timestamp_ms()})
        return True

    return await redis.transaction(_mark, in_progress_key, value_from_callable=True)


async def _abort_verdict(job: Job, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for ``job``'s outcome; ``True`` is arq's confirmed abort.

    A stored abort replays out of ``Job.result`` as its revived ``CancelledError``; a stored
    success or no retained result is ``False``. A cancellation of the CALLING task re-raises
    instead of reading as the job's abort; a stored failure and an elapsed wait
    (``TimeoutError``) propagate.
    """
    try:
        await job.result(timeout=timeout)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        return True
    except ResultNotFound:
        return False
    return False


def _ended_reply(task_id: str, status: JobStatus) -> str:
    """The reply for a job that has completed or that arq no longer holds: nothing is left to cancel."""
    if status == JobStatus.not_found:
        return f"Task {task_id} not found"
    return f"Task {task_id} cannot be canceled (status: {status.value})"


async def _abort_outcome(job: Job, task_id: str, *, timeout: float, unconfirmed: str) -> str:
    """The reply for ``job``'s outcome after its abort mark, waiting up to ``timeout`` seconds.

    See :meth:`ArqInspection.cancel`; ``unconfirmed`` opens the reply when no outcome arrives
    within the wait, which then names the status the job reached, read from arq after the wait.
    """
    try:
        aborted = await _abort_verdict(job, timeout)
    except TimeoutError:
        return f"{unconfirmed} (status: {(await job.status()).value})"
    except Exception:
        status = await job.status()
        if status == JobStatus.complete:
            # Replayed stored outcome of a task that finished during the wait —
            # not an abort failure. A revived TaskFailedError is the task's own
            # failure; a failure with no revivable detail leaves abort and
            # own-failure indistinguishable.
            info = await job.result_info()
            if info is not None and not info.success:
                detail = _failure_detail(info.result)
                if isinstance(info.result, TaskFailedError):
                    return f"Task {task_id} failed on its own before the abort could take effect: {detail}"
                return (
                    f"Task {task_id} finished in failure after the abort request "
                    f"(aborted or failed on its own): {detail}"
                )
            return _ended_reply(task_id, status)
        if status == JobStatus.not_found:
            # Vanished between replay and re-check; gone, guaranteed not to run.
            return _ended_reply(task_id, status)
        raise
    if aborted:
        # arq's confirmed-abort verdict (a replayed CancelledError).
        return f"Task {task_id} aborted"
    status = await job.status()
    if status in (JobStatus.not_found, JobStatus.complete):
        # The wait ended on a stored success / no retained result:
        # the task finished during the wait, nothing left to cancel.
        return _ended_reply(task_id, status)
    return f"{unconfirmed} (status: {status.value})"


class ArqInspection(BackendInspection):
    """arq's views of its tasks; no view narrows to one worker."""

    async def task_status(self, task_id: str) -> str:
        """The job's arq status (``not_found`` for a job arq does not hold)."""
        async with pool.arq_connection() as arq_redis:
            job = await job_handle(arq_redis, task_id)
            return (await job.status()).value

    async def task_result(self, task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
        """The task's ``(ready, status, value)``, waiting up to ``timeout`` seconds for it to complete.

        A job arq does not hold is ready with the text ``Task <id> not found``; a completed one
        whose result is no longer retained is ready with ``No result found for task <id>``. A
        failed task re-raises its stored failure as ``TaskFailedError`` carrying the original
        exception's type name, ``repr`` and traceback text (an aborted task re-raises the same
        way, its detail naming the ``CancelledError``). A successful result that could not be
        JSON-serialized is the stored tagged description (type name and ``repr``) instead.
        """
        async with pool.arq_connection() as arq_redis:
            job = await job_handle(arq_redis, task_id)
            status = await job.status()
            if status == JobStatus.not_found:
                return True, status.value, f"Task {task_id} not found"
            if timeout is None and status != JobStatus.complete:
                return False, status.value, None
            try:
                value = await wait_job_result(job, timeout=timeout)
            except TimeoutError:
                return False, (await job.status()).value, None
            except ResultNotFound:
                return True, JobStatus.complete.value, f"No result found for task {task_id}"
            return True, JobStatus.complete.value, value

    @property
    def terminates(self) -> bool:
        """``True``: arq's abort cancels the running job's task."""
        return True

    async def cancel(self, task_id: str, terminate: bool) -> str:
        """Cancel a task, waiting up to the configured task timeout for its outcome.

        With ``terminate`` the job is marked for abort whatever its state and a running job's task
        is cancelled. Without it the job is marked only while no worker has claimed it — one
        transaction watching arq's in-progress key, so a job that starts meanwhile is never marked
        — and a running job is refused with ``UnsupportedBackendTool``.

        No outcome within the wait is reported as requested-but-unconfirmed, with the status the job
        reached, read from arq after the wait. The request stays recorded: a worker never runs a
        queued job marked for abort, and cancels a running one's task at its next abort poll. A task
        that reaches a stored outcome during the wait is reported from that outcome, never as a
        cancel failure: the wait polls the result key, and a stored abort replays out of the poll as
        its revived ``CancelledError`` — arq's confirmed-abort verdict, reported as "aborted" —
        while a stored own-failure replays as its revived ``TaskFailedError``, reported as
        failed-on-its-own with the stored detail. A stored success (or a task vanishing without a
        retained result) reads back as nothing to cancel. Only a stored failure carrying no
        revivable detail (arq's last-ditch placeholder when even the tagged description could not
        serialize) stays reported as aborted-or-failed, with the raw stored value appended.

        Inside a job it requests the abort and returns without waiting: the outcome it would wait
        for may be a job queued behind the caller, which does not start while the caller runs. An
        outcome the task already has is reported as above; an abort with none yet is reported as
        requested but not awaited, so not confirmed, with the status the job has.
        """
        async with pool.arq_connection() as arq_redis:
            queue = await holding_queue(arq_redis, task_id)
            job = await job_handle(arq_redis, task_id, queue)
            status = await job.status()
            if status in (JobStatus.not_found, JobStatus.complete):
                return _ended_reply(task_id, status)
            if not await _mark_abort(arq_redis, task_id, queue, only_if_not_started=not terminate):
                raise self._unsupported("backend_cancel_task without terminate for a running task")
            if in_job():
                unconfirmed = f"Task {task_id} abort requested but not awaited inside a job, so not confirmed"
                return await _abort_outcome(job, task_id, timeout=0, unconfirmed=unconfirmed)
            unconfirmed = f"Task {task_id} abort requested but not confirmed"
            return await _abort_outcome(job, task_id, timeout=arq_settings().task_timeout, unconfirmed=unconfirmed)

    async def active_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """Each running job's id with its ``{"status": "in_progress"}``.

        arq marks a running job with an in-progress key; the job's status API is the authority (a
        lingering key after completion reads complete).
        """
        async with pool.arq_connection() as arq_redis:
            pattern = f"{in_progress_key_prefix}*".encode()
            job_ids: list[str] = []
            async for key in arq_redis.scan_iter(match=pattern):
                raw = key.decode() if isinstance(key, bytes) else key
                job_ids.append(raw.removeprefix(in_progress_key_prefix))
            tasks: dict[str, Any] = {}
            for job_id in job_ids:
                status = await in_progress_handle(arq_redis, job_id).status()
                if status == JobStatus.in_progress:
                    tasks[job_id] = {"status": status.value}
            return tasks

    async def reserved_tasks(self) -> dict[str, list[str]]:
        """Each queue's due job ids not yet picked up: the settings queue and every derived queue."""
        async with pool.arq_connection() as arq_redis:
            now_ms = timestamp_ms()
            reserved: dict[str, list[str]] = {}
            for queue in await known_queues(arq_redis):
                reserved[queue] = [
                    job.job_id
                    for job in await arq_redis.queued_jobs(queue_name=queue)
                    if job.job_id is not None and job.score is not None and job.score <= now_ms
                ]
            return reserved

    async def scheduled_tasks(self) -> dict[str, float]:
        """Each future-dated job id with its due time in epoch milliseconds (the queue zset score)."""
        async with pool.arq_connection() as arq_redis:
            now_ms = timestamp_ms()
            scheduled: dict[str, float] = {}
            for queue in await known_queues(arq_redis):
                scheduled.update(
                    {
                        job.job_id: float(job.score)
                        for job in await arq_redis.queued_jobs(queue_name=queue)
                        if job.job_id is not None and job.score is not None and job.score > now_ms
                    }
                )
            return scheduled

    async def failed_tasks(self) -> list[dict[str, Any]]:
        """The failed (aborted included) tasks whose results arq still keeps, as ``{"task_id", "error"}``.

        ``error`` is the stored failure detail (the original exception's ``repr``; an aborted
        task's names its ``CancelledError``). arq keeps job outcomes only for the configured
        keep-result window.
        """
        async with pool.arq_connection() as arq_redis:
            return [
                {"task_id": result.job_id, "error": _failure_detail(result.result)}
                for result in await arq_redis.all_job_results()
                if not result.success and result.job_id is not None
            ]
