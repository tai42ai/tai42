"""The three dispatching branch tools every backend offers, built once over its seams.

:func:`register_task_branches` registers the three BACKEND tool extensions. Each factory receives
``(func, name, description)`` and returns a new-named branch tool bound alongside the original:

- ``<tool>_sync_task`` queues the call and waits for its result;
- ``<tool>_async_task`` queues the call and replies with the job's id;
- ``<tool>_schedule_task`` writes a recurring schedule firing the tool.

A branch's signature is the tool's own parameters widened with the branch's options (built with
makefun, so a tool caller sees real parameters). A backend supplies only its seams: the
:class:`~tai42_contract.backend.dispatch.TaskDoor` its task branches queue through, and the
:class:`~tai42_contract.backend.schedule_store.ScheduleWriter` its schedule branch writes through.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Final

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.backend.dispatch import TaskDoor, TaskWaitTimeoutError
from tai42_contract.backend.schedule_store import ScheduleWriter
from tai42_contract.backend.scheduling import (
    SCHEDULE_BRANCH_SUFFIX,
    SCHEDULE_CADENCE_ADAPTER,
    SCHEDULE_CADENCE_KEY,
    SCHEDULE_EXTENSION,
    SCHEDULE_NAME_KEY,
    ScheduleRecord,
)
from tai42_contract.extensions import ExtensionKind

from tai42_kit.backend.callback import prepare_backend_kwargs
from tai42_kit.backend.job_context import assert_not_in_job
from tai42_kit.backend.schedule_writes import check_cadence_supported
from tai42_kit.backend.settings import BackendDispatchSettings
from tai42_kit.backend.task_options import BACKEND_TASK_OPTS, SCHEDULE_OPTS, split_task_options, take_callback
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.lc.signature_util import add_signature_params
from tai42_kit.utils.schedule_subject import SCHEDULE_STAMPED_DOOR_OPTS

#: The extension branching a tool into ``<tool>_sync_task``.
SYNC_TASK_EXTENSION: Final = "sync_task"
#: The extension branching a tool into ``<tool>_async_task``.
ASYNC_TASK_EXTENSION: Final = "async_task"


def register_task_branches(
    door: TaskDoor[Any],
    schedules: ScheduleWriter,
    settings: Callable[[], BackendDispatchSettings],
    label: str,
) -> None:
    """Register the sync, async and schedule branch extensions of one backend.

    ``settings`` is the backend's settings accessor, read at each dispatch (the tool-name
    argument, the sync wait's timeout). ``label`` is the backend's name, as a refusal names it.
    """
    task_opts = {**BACKEND_TASK_OPTS, **door.task_opts}

    async def submit(func: Callable[..., Any], name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Prepare the job's kwargs, split the task options out of them, and queue the job."""
        kwargs = await prepare_backend_kwargs(func, settings().tool_name_arg, name, kwargs)
        options = split_task_options(kwargs, task_opts)
        callback = take_callback(options, kwargs)
        return await door.submit(args, kwargs, options, callback)

    def sync_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
        """Branch ``func`` into ``<name>_sync_task``: queue the call and wait for its result.

        A failed job raises its failure as the backend retains it; a job unfinished at the
        settings' ``task_timeout`` raises ``TimeoutError`` (the job is left running). Inside a job it raises
        ``RuntimeError`` and queues nothing: a job chains the next one with ``callback_kwargs``.
        """
        raw_name = f"{name}_{SYNC_TASK_EXTENSION}"

        async def func_impl(*args: Any, **kwargs: Any) -> Any:
            assert_not_in_job(raw_name)
            handle = await submit(func, name, args, kwargs)
            timeout = settings().task_timeout
            try:
                # The job's change may reload this process; the recorded wait keeps the reload from severing the caller.
                async with tai42_app.lifecycle.waiting_on_fleet_work():
                    return await door.wait(handle, timeout)
            except TaskWaitTimeoutError as exc:
                raise TimeoutError(
                    f"Task {door.task_id(handle)} did not complete within {timeout} seconds; it may still be running."
                ) from exc

        sig = add_signature_params(func, task_opts, exclude_fastmcp_ctx=True)
        return _branch(func, raw_name, sig, func_impl, description)

    def async_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
        """Branch ``func`` into ``<name>_async_task``: queue the call and reply with the job's id."""
        raw_name = f"{name}_{ASYNC_TASK_EXTENSION}"
        doc = f"Async version of '{name}'. Submits the task to a background queue."
        doc += f"\n\nOriginal Doc:\n{description}" if description else ""

        async def func_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
            handle = await submit(func, name, args, kwargs)
            return {"task_id": door.task_id(handle), "status": "submitted"}

        sig = add_signature_params(func, task_opts, exclude_fastmcp_ctx=True)
        return _branch(func, raw_name, sig.replace(return_annotation=dict[str, Any]), func_impl, doc)

    def schedule_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
        """Branch ``func`` into ``<name>_schedule_task``: write a recurring schedule firing it."""
        raw_name = f"{name}{SCHEDULE_BRANCH_SUFFIX}"
        doc = f"Scheduled version of '{name}'. Schedules the task to run later via a background queue."
        doc += f"\n\nOriginal Doc:\n{description}" if description else ""

        async def func_impl(*args: Any, **kwargs: Any) -> None:
            tool_name_arg = settings().tool_name_arg
            kwargs = await prepare_backend_kwargs(func, tool_name_arg, name, kwargs, scheduled=True)
            schedule_name = kwargs.pop(SCHEDULE_NAME_KEY, None)
            if not schedule_name:
                raise ValueError(f"{SCHEDULE_NAME_KEY} is required")
            cadence = SCHEDULE_CADENCE_ADAPTER.validate_python(kwargs.pop(SCHEDULE_CADENCE_KEY))
            check_cadence_supported(schedules, cadence, label)
            # The record names the fired tool on its own; the writer stores it under the tool-name argument.
            del kwargs[tool_name_arg]
            record = ScheduleRecord(
                name=schedule_name, tool=name, args=list(args), kwargs=kwargs, cadence=cadence, enabled=True
            )
            await schedules.write(record)

        # The branch accepts its own options PLUS the reserved keys the create door stamps (identity
        # and contract), or the tool binding refuses the stamped dispatch as unexpected kwargs.
        sig = add_signature_params(func, {**SCHEDULE_OPTS, **SCHEDULE_STAMPED_DOOR_OPTS}, exclude_fastmcp_ctx=True)
        return _branch(func, raw_name, sig.replace(return_annotation=inspect.Signature.empty), func_impl, doc)

    tai42_app.extensions.extension(sync_task, kind=ExtensionKind.BACKEND, name=SYNC_TASK_EXTENSION)
    tai42_app.extensions.extension(async_task, kind=ExtensionKind.BACKEND, name=ASYNC_TASK_EXTENSION)
    tai42_app.extensions.extension(schedule_task, kind=ExtensionKind.BACKEND, name=SCHEDULE_EXTENSION)


def _branch(
    func: Callable[..., Any],
    raw_name: str,
    sig: inspect.Signature,
    func_impl: Callable[..., Any],
    doc: str,
) -> Callable[..., Any]:
    """Compose the branch tool ``raw_name`` presenting ``sig`` and running ``func_impl``."""
    safe_name = makefun_func_name(raw_name)
    branch = create_function(
        func_signature=sig,
        func_impl=func_impl,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=doc,
    )
    # The branch loop registers a branch by its ``__name__``; restore the raw
    # intended name so a hyphenated name isn't collapsed to its makefun alias.
    branch.__name__ = raw_name
    return branch
