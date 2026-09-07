"""The job functions RQ workers run, and the enqueue path that submits them.

``tool_execution`` is the generic job body every backend extension queues.
``callback_job`` chains a follow-up tool after a finished job (enqueued
``depends_on`` the primary job). ``enqueue_task`` maps the backend task options
(``eta`` / ``countdown`` / ``expires`` / ``callback_kwargs``) onto RQ's enqueue API.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from rq import Queue
from rq.job import Job
from tai42_contract.app import tai42_app
from tai42_kit.backend import CallbackSchema, callback_execution
from tai42_kit.clients import client_ctx, shutdown_all_clients
from tai42_kit.clients.impl.redis import SyncRedisClient
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run
from tai42_kit.utils.schedule_subject import schedule_state_context
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG, bind_worker_secret_capability

from tai42_backend_rq.settings import rq_settings

# Task options every backend extension appends to its branch tool's signature.
RQ_TASK_OPTS: dict[str, Any] = {
    "countdown": int | None,
    "expires": str | float | None,  # Maps to the job ttl (in seconds).
    "eta": str | None,  # ISO-format datetime string.
    "callback_kwargs": CallbackSchema | None,
}

# Schedule options the schedule_task extension appends.
RQ_SCHEDULE_OPTS: dict[str, Any] = {
    "backend_schedule_name": str,
    "backend_schedule": int | float | str | dict[str, Any],
}


async def tool_execution(*args: Any, **kwargs: Any) -> Any:
    """Run one app tool inside a worker job. The target tool name arrives under
    the ``tool_name_arg`` kwarg (its absence raises). The job's pooled clients are
    closed before its fresh event loop is torn down."""
    tool_name = kwargs.pop(rq_settings().tool_name_arg)
    # A worker executes a dequeued task with no live caller holding a
    # connection, so the turn budget does not apply, and no HTTP request bound
    # the secret-read capability — the worker binds the submitter's own capability
    # carried with the job (falling back to the gate state when none rode along).
    secret_capability = kwargs.pop(WORKER_SECRET_CAPABILITY_ARG, None)
    # A scheduled fire carries its subject under a reserved kwarg (stamped at creation);
    # popped and re-established as the ``schedule`` state context here so a state write
    # during the fire is keyed and attributed to it. A plain background run carries none
    # and runs context-free (``api`` door).
    detached_token = mark_detached_run()
    try:
        with schedule_state_context(kwargs), bind_worker_secret_capability(secret_capability):
            return await tai42_app.tools.run_tool(tool_name, kwargs, offload_sync=True)
    finally:
        reset_detached_run(detached_token)
        await shutdown_all_clients()


async def callback_job(previous_job_id: str, callback: CallbackSchema) -> Any:
    """Run ``callback`` against the finished primary job's result.

    Enqueued ``depends_on`` the primary job. A failed or not-finished primary is
    a domain outcome reported in the returned status dict; an unexpected failure
    (missing job, broken callback) raises loudly. The job's pooled clients are
    closed before its fresh event loop is torn down.
    """
    try:
        async with client_ctx(SyncRedisClient, url=rq_settings().redis_url) as r:

            def _read_outcome() -> tuple[str, Any]:
                job = Job.fetch(previous_job_id, connection=r)
                if job.is_failed:
                    # RQ persists a failed job's exception as traceback text on its
                    # latest Result (``exc_string``), not on the Result's repr.
                    result = job.latest_result()
                    exc_string = getattr(result, "exc_string", None)
                    return "failed", exc_string or str(result)
                if not job.is_finished:
                    return "not_finished", None
                return "finished", job.return_value()

            outcome, value = await asyncio.to_thread(_read_outcome)

        if outcome == "failed":
            return {"status": "failure", "job_id": previous_job_id, "error": value}
        if outcome == "not_finished":
            return {"status": "not_finished", "job_id": previous_job_id, "error": "Job not completed"}
        return await callback_execution(value, callback)
    finally:
        await shutdown_all_clients()


async def enqueue_task(*args: Any, **kwargs: Any) -> Job:
    """Enqueue a ``tool_execution`` job, honoring the backend task options.

    ``eta`` (ISO datetime) schedules via ``enqueue_at``, ``countdown`` (seconds)
    via ``enqueue_in``, ``expires`` maps to the job ``ttl``, and ``callback_kwargs``
    chains a ``callback_job`` dependent on the primary job. Returns the primary job.
    """
    async with client_ctx(SyncRedisClient, url=rq_settings().redis_url) as r:
        queue = Queue(rq_settings().queue_name, connection=r)

        task_kwargs = {k: kwargs.pop(k) for k in RQ_TASK_OPTS if k in kwargs}
        enqueue_opts = {k: v for k, v in task_kwargs.items() if v is not None}
        callback: CallbackSchema | None = enqueue_opts.pop("callback_kwargs", None)
        countdown = enqueue_opts.pop("countdown", None)
        eta_str = enqueue_opts.pop("eta", None)
        ttl = enqueue_opts.pop("expires", None)

        eta = datetime.fromisoformat(eta_str) if eta_str else None
        ttl_sec = int(float(ttl)) if ttl is not None else None

        def _enqueue() -> Job:
            # Explicit args=/kwargs= form: rq's *args/**kwargs form would consume a
            # tool parameter named like a job option (description/ttl/meta/...).
            if eta:
                job = queue.enqueue_at(eta, tool_execution, args=args, kwargs=kwargs, ttl=ttl_sec)
            elif countdown:
                job = queue.enqueue_in(
                    timedelta(seconds=countdown), tool_execution, args=args, kwargs=kwargs, ttl=ttl_sec
                )
            else:
                job = queue.enqueue(tool_execution, args=args, kwargs=kwargs, ttl=ttl_sec)

            if callback:
                queue.enqueue(callback_job, args=(job.id, callback), depends_on=job)
            return job

        # Enqueueing is a blocking Redis write; keep it off the event loop.
        return await asyncio.to_thread(_enqueue)
