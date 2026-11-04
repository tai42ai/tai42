"""The task door: the vendor's queue door the dispatching branch tools go through.

A backend implements :class:`TaskDoor` over its own queue; the platform's task branch tools
(``<tool>_sync_task``, ``<tool>_async_task``) prepare the job's kwargs, split the task options
out of them and call the door. The door knows nothing of the branch tools; the branches know
nothing of the vendor's queue. The schedule branch writes through the backend's
:class:`~tai42_contract.backend.schedule_store.ScheduleWriter` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from tai42_contract.backend.callback import CallbackSchema


class TaskWaitTimeoutError(TimeoutError):
    """Raised by :meth:`TaskDoor.wait` when the job is still unfinished at its timeout.

    Its own class, so a job that itself failed with a ``TimeoutError`` is never read as an
    unfinished one.
    """


class TaskDoor[HandleT](Protocol):
    """A backend's queue door: submit a job, name it, wait for it.

    ``HandleT`` is the vendor's own handle on a submitted job (a result object, a job record);
    only the door reads it.
    """

    @property
    def task_opts(self) -> Mapping[str, Any]:
        """The vendor's own task options, by name and annotation, beyond the platform's set.

        Each becomes a keyword-only argument (default ``None``) of the sync and async branch
        tools, and reaches :meth:`submit` in its ``options`` when the caller gave a value.
        """
        ...

    async def submit(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        options: dict[str, Any],
        callback: CallbackSchema | None,
    ) -> HandleT:
        """Queue one job running the dispatched tool with ``args`` and ``kwargs``; return its handle.

        ``kwargs`` are the prepared job kwargs (the tool name and the reserved stamps inside).
        ``options`` are the task options the caller gave, ``None`` values already dropped and the
        callback taken out: the door maps each onto its queue's API and raises on one it cannot
        honour. ``callback``, when given, already carries what its follow-up job needs; the door
        chains it to run on the job's result.
        """
        ...

    def task_id(self, handle: HandleT) -> str:
        """The id a caller looks the submitted job up by."""
        ...

    async def wait(self, handle: HandleT, timeout: float) -> Any:
        """Wait up to ``timeout`` seconds for the job to finish and return its result.

        A failed job raises its failure as the vendor retains it. A job still unfinished at
        ``timeout`` raises :class:`TaskWaitTimeoutError`; the job itself is left running.
        """
        ...
