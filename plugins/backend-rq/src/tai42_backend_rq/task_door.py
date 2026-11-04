"""The RQ task door: a dispatched tool queued as a ``tool_execution`` job.

The task options map onto RQ's enqueue API: ``eta`` (ISO datetime) through ``enqueue_at``,
``countdown`` (seconds) through ``enqueue_in``, ``expires`` onto the job ``ttl``, and ``queue`` (a
label) onto ``<RQ_QUEUE_NAME>.<label>``. A callback becomes a ``callback_job`` enqueued
``depends_on`` the job.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from tai42_contract.backend import CallbackSchema, TaskWaitTimeoutError
from tai42_kit.backend import derived_queue
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import SyncRedisClient

from tai42_backend_rq.job_status import ENDED_STATUSES
from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.settings import rq_client_kwargs, rq_settings
from tai42_backend_rq.tasks import callback_job, tool_execution

#: RQ's own task options beyond the platform's set: none.
RQ_TASK_OPTS: Mapping[str, Any] = {}

#: Seconds between two reads of a waited-on job's status.
_POLL_INTERVAL_SECONDS = 0.5


class RqTaskDoor:
    """Queue a dispatched tool on RQ and poll its job for the result."""

    @property
    def task_opts(self) -> Mapping[str, Any]:
        """RQ's own task options (:data:`RQ_TASK_OPTS`)."""
        return RQ_TASK_OPTS

    async def submit(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        options: dict[str, Any],
        callback: CallbackSchema | None,
    ) -> Job:
        """Enqueue ``tool_execution`` on the option's queue; a label outside the grammar raises first."""
        queue_name = derived_queue(rq_settings().queue_name, options.get("queue"))
        countdown = options.get("countdown")
        eta_str = options.get("eta")
        expires = options.get("expires")
        eta = datetime.fromisoformat(eta_str) if eta_str else None
        ttl = int(float(expires)) if expires is not None else None

        async with client_ctx(SyncRedisClient, **rq_client_kwargs()) as r:
            queue = TaskBudgetQueue(queue_name, connection=r)

            def _enqueue() -> Job:
                # Explicit args=/kwargs= form: rq's *args/**kwargs form would consume a
                # tool parameter named like a job option (description/ttl/meta/...).
                if eta:
                    job = queue.enqueue_at(eta, tool_execution, args=args, kwargs=kwargs, ttl=ttl)
                elif countdown:
                    job = queue.enqueue_in(
                        timedelta(seconds=countdown), tool_execution, args=args, kwargs=kwargs, ttl=ttl
                    )
                else:
                    job = queue.enqueue(tool_execution, args=args, kwargs=kwargs, ttl=ttl)
                if callback is not None:
                    queue.enqueue(callback_job, args=(job.id, callback), depends_on=job)
                return job

            # Enqueueing is a blocking Redis write; keep it off the event loop.
            return await asyncio.to_thread(_enqueue)

    def task_id(self, handle: Job) -> str:
        """The RQ job id."""
        return handle.id

    async def wait(self, handle: Job, timeout: float) -> Any:
        """Poll the job off the loop until it ends.

        A finished job returns its result. A job that vanished, failed, or ended without a result
        raises ``RuntimeError``: a failure carries RQ's stored traceback text (the only failure
        detail the broker retains), and a stopped or cancelled job names its status. Only a job
        still unfinished at ``timeout`` raises :class:`TaskWaitTimeoutError`.
        """
        return await asyncio.to_thread(_poll_job_result, handle, timeout)


def _poll_job_result(job: Job, timeout: float) -> Any:
    """Poll ``job`` until it ends; return its result or raise precisely. Blocking Redis reads."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            job.refresh()
        except NoSuchJobError as exc:
            raise RuntimeError(f"Job {job.id} not found.") from exc
        status = job.get_status()
        if status == JobStatus.FINISHED:
            return job.return_value()
        if status == JobStatus.FAILED:
            result = job.latest_result()
            exc_string = getattr(result, "exc_string", None)
            raise RuntimeError(f"Job {job.id} failed: {exc_string or result}")
        if status in ENDED_STATUSES:
            raise RuntimeError(f"Job {job.id} did not complete (status: {JobStatus(status).value})")
        if time.monotonic() >= deadline:
            raise TaskWaitTimeoutError(f"job {job.id} still {JobStatus(status).value} after {timeout} seconds")
        time.sleep(_POLL_INTERVAL_SECONDS)
