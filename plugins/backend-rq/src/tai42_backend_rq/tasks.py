"""The job functions RQ workers run.

``tool_execution`` is the generic job body every dispatching branch tool queues.
``callback_job`` chains a follow-up tool after a finished job (enqueued
``depends_on`` the primary job).
"""

from __future__ import annotations

import asyncio
from typing import Any

from rq.job import Job, JobStatus
from tai42_kit.backend import CallbackSchema, callback_execution
from tai42_kit.backend.job_run import run_dispatched_tool
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import SyncRedisClient

from tai42_backend_rq.settings import rq_client_kwargs, rq_settings


async def tool_execution(*args: Any, **kwargs: Any) -> Any:
    """Run one app tool inside a worker job.

    The target tool name arrives under the ``tool_name_arg`` kwarg (its absence raises). The loop
    the job runs on, and the release of the kit's resources bound to it, are its job class's
    (:mod:`tai42_backend_rq.job_loop`).
    """
    return await run_dispatched_tool(kwargs, tool_name_arg=rq_settings().tool_name_arg)


async def callback_job(previous_job_id: str, callback: CallbackSchema) -> Any:
    """Run ``callback`` against the finished primary job's result.

    Enqueued ``depends_on`` the primary job, so RQ runs it only once that job succeeded. A
    primary found failed or unfinished raises ``RuntimeError`` (a failed one carrying RQ's stored
    traceback text), as does a missing one and a broken callback: RQ records this job failed. The
    loop the job runs on, and the release of the kit's resources bound to it, are its job class's
    (:mod:`tai42_backend_rq.job_loop`).
    """
    async with client_ctx(SyncRedisClient, **rq_client_kwargs()) as r:

        def _read_result() -> Any:
            job = Job.fetch(previous_job_id, connection=r)
            if job.is_failed:
                # RQ persists a failed job's exception as traceback text on its
                # latest Result (``exc_string``), not on the Result's repr.
                result = job.latest_result()
                exc_string = getattr(result, "exc_string", None)
                raise RuntimeError(f"callback predecessor job {previous_job_id} failed: {exc_string or result}")
            if not job.is_finished:
                raise RuntimeError(
                    f"callback predecessor job {previous_job_id} is still {JobStatus(job.get_status()).value}"
                )
            return job.return_value()

        value = await asyncio.to_thread(_read_result)

    return await callback_execution(value, callback)
