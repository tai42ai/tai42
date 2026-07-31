"""The three BACKEND-kind tool extensions.

Each branches a tool onto the arq queue under a new name: ``sync_task`` (queue
and wait for the result), ``async_task`` (queue and return the task id), and
``schedule_task`` (register a recurring schedule that queues the tool). All
present a real composed signature via :func:`makefun.create_function` — the
tool's own parameters plus the queue/schedule options.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import orjson
from makefun import create_function
from pydantic_core import to_jsonable_python
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_arq.callback import prepare_backend_kwargs
from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.records import derive_cron_or_interval, next_run_after
from tai42_backend_arq.scheduler import safe_schedule_transition, wait_job_result
from tai42_backend_arq.settings import arq_settings
from tai42_backend_arq.signatures import add_signature_params
from tai42_backend_arq.tasks import ARQ_SCHEDULE_OPTS, ARQ_TASK_OPTS, enqueue_task


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND)
def sync_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_sync_task``: queue the tool and wait (up to
    ``task_timeout``) for its result. A failed job re-raises as
    ``TaskFailedError``; an unserializable success returns its tagged
    description instead of the value."""
    new_name = f"{name}_sync_task"
    sig = add_signature_params(func, ARQ_TASK_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        kwargs = await prepare_backend_kwargs(func, arq_settings().tool_name_arg, name, kwargs)
        arq_redis = await RedisPoolManager.get()

        job = await enqueue_task(arq_redis, *args, **kwargs)
        timeout = arq_settings().task_timeout

        try:
            return await wait_job_result(job, timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"Job {job.job_id} did not complete within {timeout} seconds.") from None

    return create_function(
        func_signature=sig,
        func_impl=func_impl,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=description,
    )


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND)
def schedule_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into a ``<name>_schedule_task`` variant that registers a
    recurring schedule (interval or crontab) queueing the tool."""
    new_name = f"{name}_schedule_task"
    new_description = f"Scheduled version of '{name}'. Schedules the task to run later via a background queue."
    new_description += f"\n\nOriginal Doc:\n{description}" if description else ""

    sig = add_signature_params(func, ARQ_SCHEDULE_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> None:
        kwargs = await prepare_backend_kwargs(func, arq_settings().tool_name_arg, name, kwargs)

        arq_redis = await RedisPoolManager.get()
        schedule_name = kwargs.pop("backend_schedule_name", None)
        schedule_in = kwargs.pop("backend_schedule", None)
        if not schedule_name:
            raise ValueError("backend_schedule_name is required")
        if schedule_in is None:
            raise ValueError("backend_schedule is required")

        norm = normalize_schedule(schedule_in)
        cron_or_interval = derive_cron_or_interval(norm)
        defer_by, last_scheduled_ts = next_run_after(cron_or_interval, datetime.now(UTC))

        mapping_updates = {
            "target": "tool_execution",
            "args": orjson.dumps(to_jsonable_python(list(args))),
            "kwargs": orjson.dumps(to_jsonable_python(kwargs)),
            "schedule": orjson.dumps(norm),
            "cron_or_interval": str(cron_or_interval),
            "enabled": "true",
        }

        await safe_schedule_transition(
            arq_redis,
            schedule_name,
            defer_by=defer_by,
            last_scheduled_ts=last_scheduled_ts,
            mapping_updates=mapping_updates,
            enforce_job_id=None,
        )

    return create_function(
        func_signature=sig.replace(return_annotation=inspect.Signature.empty),
        func_impl=func_impl,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=new_description,
    )


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND)
def async_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into a ``<name>_async_task`` variant that queues the tool
    and returns immediately with the task id."""
    new_name = f"{name}_async_task"
    new_description = f"Async version of '{name}'. Submits the task to a background queue."
    new_description += f"\n\nOriginal Doc:\n{description}" if description else ""

    sig = add_signature_params(func, ARQ_TASK_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs = await prepare_backend_kwargs(func, arq_settings().tool_name_arg, name, kwargs)

        arq_redis = await RedisPoolManager.get()
        job = await enqueue_task(arq_redis, *args, **kwargs)
        return {"task_id": job.job_id, "status": "submitted"}

    return create_function(
        func_signature=sig.replace(return_annotation=dict[str, Any]),
        func_impl=func_impl,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=new_description,
    )
