"""The Celery task door: a dispatched tool queued as a ``tool_execution`` task.

The task options map onto ``apply_async``'s own keywords, ``queue`` (a label) onto
``<CELERY_QUEUE_NAME>.<label>``; a callback becomes a ``link`` signature running
``callback_task`` on the task's result, on the task's own queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.result import AsyncResult
from tai42_contract.backend import CallbackSchema, TaskWaitTimeoutError
from tai42_kit.backend import derived_queue

from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.tasks import callback_task, tool_execution

#: Celery's own task options beyond the platform's set, each an ``apply_async`` keyword.
CELERY_TASK_OPTS: Mapping[str, Any] = {
    "priority": int | None,
    "retry": bool | None,
    "routing_key": str | None,
}


class CeleryTaskDoor:
    """Queue a dispatched tool on Celery and wait on its ``AsyncResult``."""

    @property
    def task_opts(self) -> Mapping[str, Any]:
        """Celery's own task options (:data:`CELERY_TASK_OPTS`)."""
        return CELERY_TASK_OPTS

    async def submit(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        options: dict[str, Any],
        callback: CallbackSchema | None,
    ) -> AsyncResult:
        """Send ``tool_execution`` with the options as ``apply_async`` keywords; chain the callback by ``link``.

        The callback is sent to the task's own queue, so the worker serving the task's label runs it.
        A queue label outside the grammar raises ``ValueError`` before anything is sent.
        """
        queue = derived_queue(celery_settings().queue_name, options.get("queue"))
        send_options = {**options, "queue": queue}
        if callback is not None:
            send_options["link"] = callback_task.s(callback).set(queue=queue)
        return tool_execution.apply_async(args=args, kwargs=kwargs, **send_options)

    def task_id(self, handle: AsyncResult) -> str:
        """The Celery task id."""
        # ``AsyncResult`` declares ``id = None`` at class level but refuses a ``None`` id when built.
        return cast(str, handle.id)

    async def wait(self, handle: AsyncResult, timeout: float) -> Any:
        """Block off the loop on the task's result; the task's own exception propagates unwrapped."""
        try:
            return await asyncio.to_thread(handle.get, timeout=timeout)
        except CeleryTimeoutError as exc:
            raise TaskWaitTimeoutError(f"task {handle.id} still {handle.status} after {timeout} seconds") from exc
