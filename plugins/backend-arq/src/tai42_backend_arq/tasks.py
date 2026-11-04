"""Worker functions: the dispatched tool's job and the callback job chained onto it.

``tool_execution`` runs one tool by name and, when a callback schema rode along and the tool
succeeded, chains a ``callback_job`` over this job's result.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from arq.jobs import JobStatus
from tai42_kit.backend import CALLBACK_OPTION, CallbackSchema, callback_execution
from tai42_kit.backend.job_run import run_dispatched_tool

from tai42_backend_arq.jobs import job_handle
from tai42_backend_arq.scheduler import wait_job_result
from tai42_backend_arq.settings import arq_settings


async def callback_job(
    ctx: dict[str, Any],
    previous_job_id: str,
    callback: CallbackSchema | dict[str, Any],
) -> Any:
    """Wait for ``previous_job_id`` to complete, then run ``callback`` over its result.

    A predecessor that is missing raises ``RuntimeError``, one still unfinished at the settings'
    ``callback_timeout`` raises ``TimeoutError``, and one that failed raises its
    ``TaskFailedError``: arq records this callback a failed job in each case, as it does a
    failure of the callback's own execution.
    """
    # The schema crosses the queue as JSON, so it arrives as a plain mapping.
    if not isinstance(callback, CallbackSchema):
        callback = CallbackSchema.model_validate(callback)
    job = await job_handle(ctx["redis"], previous_job_id)
    timeout = arq_settings().callback_timeout
    start_time = time.time()

    while True:
        status = await job.status()
        if status == JobStatus.complete:
            break
        if status == JobStatus.not_found:
            raise RuntimeError(f"callback predecessor job {previous_job_id} not found")
        if (time.time() - start_time) > timeout:
            raise TimeoutError(
                f"callback predecessor job {previous_job_id} still {status.value} after {timeout} seconds"
            )
        await asyncio.sleep(0.1)

    result = await wait_job_result(job)
    return await callback_execution(result, callback)


async def tool_execution(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    """Run the tool named by the ``tool_name_arg`` kwarg.

    When ``callback_kwargs`` rode along and the tool succeeded, chain a callback job keyed to
    this job's id; a tool that raised chains nothing.
    """
    callback = kwargs.pop(CALLBACK_OPTION, None)
    result = await run_dispatched_tool(kwargs, tool_name_arg=arq_settings().tool_name_arg)
    if callback is not None:
        # The worker pool's default queue is this worker's queue: the job's own.
        await ctx["redis"].enqueue_job("callback_job", ctx["job_id"], callback)
    return result
