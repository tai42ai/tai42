"""The three BACKEND tool extensions: ``sync_task`` / ``schedule_task`` /
``async_task``.

Each factory is handed a tool's callable, name, and description and returns a
new-named branch tool that routes the call through the RQ queue. The branch
presents the tool's own signature plus the backend task/schedule options (built
with makefun); the queued job dispatches back onto the original tool by name via
``tool_execution``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Protocol

from makefun import create_function
from rq.exceptions import NoSuchJobError
from rq.job import JobStatus
from rq_scheduler import Scheduler
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.backend import prepare_backend_kwargs
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import SyncRedisClient
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_rq.schedules import apply_normalized_schedule
from tai42_backend_rq.settings import rq_settings
from tai42_backend_rq.signatures import add_signature_params
from tai42_backend_rq.tasks import RQ_SCHEDULE_OPTS, RQ_TASK_OPTS, enqueue_task, tool_execution


class _PollableJob(Protocol):
    """The slice of the RQ job API the result poll reads."""

    @property
    def id(self) -> str: ...

    def refresh(self) -> None: ...

    def get_status(self) -> str: ...

    def return_value(self) -> Any: ...

    def latest_result(self) -> Any: ...


def _wait_for_job_result(job: _PollableJob, timeout: float) -> Any:
    """Poll a job until it finishes; return its result or raise precisely.

    Raises ``RuntimeError`` when the job vanished or failed (carrying RQ's stored
    traceback text, the only failure detail the broker retains) and
    ``TimeoutError`` when ``timeout`` seconds elapse first. Blocking Redis
    polling — callers run it off the event loop.
    """
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
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Job {job.id} did not complete within {timeout} seconds (status: {status}).")
        time.sleep(0.5)  # Poll interval.


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="sync_task")
def sync_task(func: Any, name: str, description: str) -> Any:
    """Branch ``func`` into ``<name>_sync_task``: queue the call and block
    until the job finishes, returning the job's result."""
    raw_name = f"{name}_sync_task"
    safe_name = makefun_func_name(raw_name)
    sig = add_signature_params(func, RQ_TASK_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        kwargs = await prepare_backend_kwargs(func, rq_settings().tool_name_arg, name, kwargs)
        job = await enqueue_task(*args, **kwargs)
        timeout = rq_settings().task_timeout
        return await asyncio.to_thread(_wait_for_job_result, job, timeout)

    branch = create_function(
        func_signature=sig,
        func_impl=func_impl,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=description,
    )
    # The branch loop registers a branch by its ``__name__``; restore the raw
    # intended name so a hyphenated name isn't collapsed to its makefun alias.
    branch.__name__ = raw_name
    return branch


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="schedule_task")
def schedule_task(func: Any, name: str, description: str) -> Any:
    """Branch ``func`` into ``<name>_schedule_task``: register a recurring
    schedule (interval or crontab) that runs the tool via the queue."""
    raw_name = f"{name}_schedule_task"
    safe_name = makefun_func_name(raw_name)
    new_description = f"Scheduled version of '{name}'. Schedules the task to run later via a background queue."
    new_description += f"\n\nOriginal Doc:\n{description}" if description else ""

    sig = add_signature_params(func, RQ_SCHEDULE_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> None:
        kwargs = await prepare_backend_kwargs(func, rq_settings().tool_name_arg, name, kwargs)

        schedule_name = kwargs.pop("backend_schedule_name", None)
        schedule_in = kwargs.pop("backend_schedule", None)
        if not schedule_name:
            raise ValueError("backend_schedule_name is required")
        if schedule_in is None:
            raise ValueError("backend_schedule is required")
        norm = normalize_schedule(schedule_in)

        async with client_ctx(SyncRedisClient, url=rq_settings().redis_url) as r:
            scheduler = Scheduler(connection=r)
            await apply_normalized_schedule(scheduler, norm, tool_execution, list(args), kwargs, schedule_name)

    branch = create_function(
        func_signature=sig.replace(return_annotation=inspect.Signature.empty),
        func_impl=func_impl,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=new_description,
    )
    # The branch loop registers a branch by its ``__name__``; restore the raw
    # intended name so a hyphenated name isn't collapsed to its makefun alias.
    branch.__name__ = raw_name
    return branch


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="async_task")
def async_task(func: Any, name: str, description: str) -> Any:
    """Branch ``func`` into ``<name>_async_task``: queue the call and return
    the job id immediately (poll with ``backend_task_result``)."""
    raw_name = f"{name}_async_task"
    safe_name = makefun_func_name(raw_name)
    new_description = f"Async version of '{name}'. Submits the task to a background queue."
    new_description += f"\n\nOriginal Doc:\n{description}" if description else ""

    sig = add_signature_params(func, RQ_TASK_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs = await prepare_backend_kwargs(func, rq_settings().tool_name_arg, name, kwargs)
        job = await enqueue_task(*args, **kwargs)
        return {"task_id": job.id, "status": "submitted"}

    branch = create_function(
        func_signature=sig.replace(return_annotation=dict[str, Any]),
        func_impl=func_impl,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=new_description,
    )
    # The branch loop registers a branch by its ``__name__``; restore the raw
    # intended name so a hyphenated name isn't collapsed to its makefun alias.
    branch.__name__ = raw_name
    return branch
