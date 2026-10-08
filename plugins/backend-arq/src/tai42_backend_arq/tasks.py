"""Worker functions and the enqueue helper.

``tool_execution`` runs one tool by name and, when a callback schema rode along,
chains a ``callback_job`` over this job's result. ``enqueue_task`` maps the
backend task options (``eta`` / ``countdown`` / ``expires`` / ``callback_kwargs``)
onto arq's enqueue API.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus
from tai42_kit.backend import CallbackSchema, backend_fire, callback_execution, carry_forwarded_fire
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG, bind_worker_secret_capability

from tai42_backend_arq.scheduler import wait_job_result
from tai42_backend_arq.settings import TaskFailedError, arq_settings, job_deserializer

# Task options every backend extension appends to its branch tool's signature.
ARQ_TASK_OPTS: dict[str, Any] = {
    "countdown": int | None,
    "expires": str | float | None,  # Seconds the queued job stays runnable.
    "eta": str | None,  # ISO-format datetime string.
    "callback_kwargs": CallbackSchema | None,
}

ARQ_SCHEDULE_OPTS: dict[str, Any] = {
    "backend_schedule_name": str,
    "backend_schedule": int | float | str | dict[str, Any],
}


async def callback_job(
    ctx: dict[str, Any],
    previous_job_id: str,
    callback: CallbackSchema | dict[str, Any],
) -> Any:
    """Wait for ``previous_job_id`` to complete, then run ``callback`` over its result.

    A missing, timed-out, or failed predecessor is a domain outcome returned as a
    status dict. An unexpected polling error, and any failure of the callback's own
    execution, raise so arq records this callback a failed job.
    """
    # The schema crosses the queue as JSON, so it arrives as a plain mapping.
    if not isinstance(callback, CallbackSchema):
        callback = CallbackSchema.model_validate(callback)
    job = Job(previous_job_id, ctx["redis"], _deserializer=job_deserializer)
    timeout = arq_settings().callback_timeout
    start_time = time.time()

    while True:
        status = await job.status()

        if status == JobStatus.complete:
            break

        if status == JobStatus.not_found:
            return {"status": "error", "job_id": previous_job_id, "error": "Job not found"}

        if (time.time() - start_time) > timeout:
            return {
                "status": "not_finished",
                "job_id": previous_job_id,
                "error": f"Job status '{status}' did not complete within {timeout}s",
            }

        await asyncio.sleep(0.1)

    try:
        # wait_job_result surfaces a failed or aborted predecessor as TaskFailedError:
        # the predecessor's own failure is a domain outcome reported as a status dict.
        result = await wait_job_result(job)
    except TaskFailedError as e:
        return {"status": "failure", "job_id": previous_job_id, "error": repr(e)}

    # The follow-up delivery is left to raise into arq's failed-job machinery.
    return await callback_execution(result, callback)


async def tool_execution(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    """Run the tool named by the ``tool_name_arg`` kwarg.

    When ``callback_kwargs`` rode along, chain a callback job keyed to this job's id — even
    when the tool raised, so the callback can react to the failure.
    """
    callback = kwargs.pop("callback_kwargs", None)

    try:
        tool_name = kwargs.pop(arq_settings().tool_name_arg)
        # A worker executes a dequeued task with no live caller holding a
        # connection, so the turn budget does not apply, and no HTTP request bound
        # the secret-read capability — the worker binds the one the job carried (a job
        # without it raises ``KeyError``).
        secret_capability = kwargs.pop(WORKER_SECRET_CAPABILITY_ARG)
        # ``backend_fire`` pops the reserved ``backend_schedule_*`` job kwargs and either binds the
        # stamped firing identity and drives the schedule door (a contract-bearing schedule, or a task
        # that forwarded the ambient subject/identity), or runs the tool plainly when the job carries no
        # door signal. A park comes back as the re-park sentinel it returns.
        detached_token = mark_detached_run()
        try:
            with bind_worker_secret_capability(secret_capability):
                return await backend_fire(tool_name, kwargs)
        finally:
            reset_detached_run(detached_token)
    finally:
        if callback:
            await ctx["redis"].enqueue_job("callback_job", ctx["job_id"], callback)


async def enqueue_task(arq_redis: ArqRedis, *args: Any, **kwargs: Any) -> Any:
    """Enqueue one ``tool_execution`` job, honoring the backend task options.

    ``eta`` (ISO datetime) defers until that moment, ``countdown`` (seconds)
    defers by that long, ``expires`` (seconds) bounds how long the job stays
    runnable, ``callback_kwargs`` stays a job kwarg. ``None`` options are dropped.
    """
    task_kwargs = {k: kwargs.pop(k) for k in ARQ_TASK_OPTS if k in kwargs}
    opts = {k: v for k, v in task_kwargs.items() if v is not None}

    callback = opts.pop("callback_kwargs", None)
    if callback is not None:
        carry_forwarded_fire(callback, kwargs)
        kwargs["callback_kwargs"] = callback

    enqueue_opts: dict[str, Any] = {}
    eta = opts.pop("eta", None)
    countdown = opts.pop("countdown", None)
    expires = opts.pop("expires", None)
    if eta:
        enqueue_opts["_defer_until"] = datetime.fromisoformat(eta)
    elif countdown:
        enqueue_opts["_defer_by"] = countdown
    if expires is not None:
        enqueue_opts["_expires"] = int(float(expires))

    return await arq_redis.enqueue_job("tool_execution", *args, **kwargs, **enqueue_opts)
