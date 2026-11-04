"""The arq task door: a dispatched tool queued as a ``tool_execution`` job.

The task options map onto arq's enqueue API: ``eta`` (ISO datetime) onto ``_defer_until``,
``countdown`` (seconds) onto ``_defer_by``, ``expires`` (seconds) onto ``_expires``, and ``queue``
(a label) onto ``<ARQ_QUEUE_NAME>.<label>``. A callback rides as the job's ``callback_kwargs``;
``tool_execution`` chains a ``callback_job`` from it once the tool succeeds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from arq.jobs import Job
from tai42_contract.backend import CallbackSchema, TaskWaitTimeoutError
from tai42_kit.backend import CALLBACK_OPTION, derived_queue

from tai42_backend_arq import pool
from tai42_backend_arq.jobs import register_queue
from tai42_backend_arq.scheduler import wait_job_result
from tai42_backend_arq.settings import arq_settings

#: arq's own task options beyond the platform's set: none.
ARQ_TASK_OPTS: Mapping[str, Any] = {}


@dataclass(frozen=True)
class ArqJobRef:
    """A queued arq job: its id and the queue it was sent to."""

    job_id: str
    queue_name: str


class ArqTaskDoor:
    """Queue a dispatched tool on arq and wait on its job for the result."""

    @property
    def task_opts(self) -> Mapping[str, Any]:
        """Arq's own task options (:data:`ARQ_TASK_OPTS`)."""
        return ARQ_TASK_OPTS

    async def submit(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        options: dict[str, Any],
        callback: CallbackSchema | None,
    ) -> ArqJobRef:
        """Enqueue ``tool_execution`` on the option's queue; a label outside the grammar raises first.

        arq refusing the enqueue (a job of the same id already exists) raises ``RuntimeError``.
        """
        queue_name = derived_queue(arq_settings().queue_name, options.get("queue"))
        enqueue_opts: dict[str, Any] = {}
        if eta := options.get("eta"):
            enqueue_opts["_defer_until"] = datetime.fromisoformat(eta)
        elif countdown := options.get("countdown"):
            enqueue_opts["_defer_by"] = countdown
        if (expires := options.get("expires")) is not None:
            enqueue_opts["_expires"] = int(float(expires))
        job_kwargs = kwargs if callback is None else {**kwargs, CALLBACK_OPTION: callback}

        async with pool.arq_connection() as arq_redis:
            # Registered before the enqueue, so a by-id lookup never misses a sent job.
            await register_queue(arq_redis, queue_name)
            job = await arq_redis.enqueue_job(
                "tool_execution", *args, _queue_name=queue_name, **job_kwargs, **enqueue_opts
            )
        if job is None:
            raise RuntimeError(f"arq refused to enqueue the job on queue {queue_name!r}: its id already exists")
        return ArqJobRef(job_id=job.job_id, queue_name=queue_name)

    def task_id(self, handle: ArqJobRef) -> str:
        """The arq job id."""
        return handle.job_id

    async def wait(self, handle: ArqJobRef, timeout: float) -> Any:
        """Wait for the job's result; a failed or aborted job raises ``TaskFailedError``.

        A job's own failure always arrives as ``TaskFailedError``, so a ``TimeoutError`` here is
        the wait running out on an unfinished job.
        """
        async with pool.arq_connection() as arq_redis:
            job = Job(handle.job_id, arq_redis, _queue_name=handle.queue_name, _deserializer=arq_redis.job_deserializer)
            try:
                return await wait_job_result(job, timeout=timeout)
            except TimeoutError as exc:
                raise TaskWaitTimeoutError(f"job {handle.job_id} still unfinished after {timeout} seconds") from exc
