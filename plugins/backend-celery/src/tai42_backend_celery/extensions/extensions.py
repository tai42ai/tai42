"""The three BACKEND tool extensions: ``sync_task``, ``schedule_task``,
``async_task``.

Each factory receives ``(func, name, description)`` and returns a new-named branch
tool bound alongside the original: ``<tool>_sync_task`` dispatches and blocks for
the result, ``<tool>_async_task`` dispatches and returns the task id, and
``<tool>_schedule_task`` persists a RedBeat entry. The branch signature extends
the tool's parameters with the Celery task or schedule options (built with
makefun); dispatch injects the tool name and the worker-side ``tool_execution``
runs it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.schedules import crontab, schedule
from makefun import create_function
from pydantic_core import to_jsonable_python
from redbeat import RedBeatSchedulerEntry
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.backend import CallbackSchema, prepare_backend_kwargs
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.signatures import add_signature_params
from tai42_backend_celery.core.tasks import CELERY_SCHEDULE_OPTS, CELERY_TASK_OPTS, callback_task, tool_execution

logger = logging.getLogger(__name__)


def _apply_task_opts(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Split the Celery task options out of the tool kwargs; a callback schema
    becomes a ``link`` signature running ``callback_task`` on the result."""
    task_kwargs = {k: kwargs.pop(k) for k in CELERY_TASK_OPTS if k in kwargs}
    apply_async_opts = {k: v for k, v in task_kwargs.items() if v is not None}
    callback: CallbackSchema | None = apply_async_opts.pop("callback_kwargs", None)
    if callback:
        apply_async_opts["link"] = callback_task.s(callback)
    return apply_async_opts


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="sync_task")
def sync_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_sync_task``: dispatch and wait for the result."""
    raw_name = f"{name}_sync_task"
    safe_name = makefun_func_name(raw_name)
    sig = add_signature_params(func, CELERY_TASK_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        kwargs = await prepare_backend_kwargs(func, celery_settings().tool_name_arg, name, kwargs)
        apply_async_opts = _apply_task_opts(kwargs)
        result = tool_execution.apply_async(args=args, kwargs=kwargs, **apply_async_opts)
        try:
            # A failed task re-raises its own exception here, never wrapped.
            return await asyncio.to_thread(result.get, timeout=celery_settings().task_timeout)
        except CeleryTimeoutError as e:
            raise TimeoutError(
                f"Task {result.id} did not complete within {celery_settings().task_timeout} seconds. "
                f"The task may still be running (status: {result.status})."
            ) from e

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
def schedule_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_schedule_task``: persist a RedBeat entry."""
    raw_name = f"{name}_schedule_task"
    safe_name = makefun_func_name(raw_name)
    new_description = f"Scheduled version of '{name}'. Schedules the task to run later via a background queue."
    new_description += f"\n\nOriginal Doc:\n{description}" if description else ""
    sig = add_signature_params(func, CELERY_SCHEDULE_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> None:
        schedule_name = kwargs.pop("backend_schedule_name", None)
        schedule_in = kwargs.pop("backend_schedule", None)
        if not schedule_name:
            raise ValueError("backend_schedule_name is required")
        if schedule_in is None:
            raise ValueError("backend_schedule is required")
        kwargs = await prepare_backend_kwargs(func, celery_settings().tool_name_arg, name, kwargs, scheduled=True)

        norm = normalize_schedule(schedule_in)
        if norm["__type__"] == "interval":
            rb_schedule = schedule(
                run_every=timedelta(seconds=float(norm["every"])),
                relative=norm.get("relative", False),
            )
        elif norm["__type__"] == "crontab":
            rb_schedule = crontab(
                minute=norm["minute"],
                hour=norm["hour"],
                day_of_week=norm["day_of_week"],
                day_of_month=norm["day_of_month"],
                month_of_year=norm["month_of_year"],
            )
        else:
            raise ValueError(f"Unsupported schedule type: {norm['__type__']}")

        entry = RedBeatSchedulerEntry(
            name=schedule_name,
            task="celery.tool_execution",
            schedule=rb_schedule,
            args=to_jsonable_python(args) if args else [],
            kwargs=to_jsonable_python(kwargs) if kwargs else {},
            app=celery_app,
        )

        await asyncio.to_thread(entry.save)

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
def async_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_async_task``: dispatch, return the task id."""
    raw_name = f"{name}_async_task"
    safe_name = makefun_func_name(raw_name)
    new_description = f"Async version of '{name}'. Submits the task to a background queue."
    new_description += f"\n\nOriginal Doc:\n{description}" if description else ""
    sig = add_signature_params(func, CELERY_TASK_OPTS, exclude_fastmcp_ctx=True)

    async def func_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs = await prepare_backend_kwargs(func, celery_settings().tool_name_arg, name, kwargs)
        apply_async_opts = _apply_task_opts(kwargs)
        result = tool_execution.apply_async(args=args, kwargs=kwargs, **apply_async_opts)
        return {"task_id": result.id, "status": "submitted"}

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
