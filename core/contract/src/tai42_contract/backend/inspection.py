"""The worker inspection: the vendor's read-only views of its workers and their tasks.

A backend implements :class:`WorkerInspection` over its own engine; the platform's task and
worker tools (ping, active workers, task status and result, cancel, active, reserved, scheduled,
registered and failed tasks, worker stats and queues) answer through it. A view of the workers
or their tasks returns a JSON object keyed as the vendor keys it (by worker, queue or task), and
the failed tasks view a list of JSON objects, one per task; the data inside is the vendor's own
shape. The view tools declare these types, so every view's reply reaches an MCP client as
structured content. A backend that cannot answer a view refuses it loudly
rather than answering empty; one that cannot narrow a view to one worker, or does not serve the
stop of a running task, says so through :attr:`WorkerInspection.addresses_workers` and
:attr:`WorkerInspection.terminates`, and such a call is refused before it reaches the view. A
cancel without ``terminate`` never stops a running task; a backend that cannot cancel a running
task without stopping it refuses that call. Every reply of a cancel says what was done: when a
worker answers that it cannot stop the task it runs, the call is refused rather than answering
that it did, and a stop the workers did not confirm is reported as not confirmed.
"""

from __future__ import annotations

from typing import Any, Protocol


class WorkerInspection(Protocol):
    """A backend's read-only views of its workers and their tasks, plus cancelling a task.

    ``worker_name`` narrows a view to one worker; ``None`` covers every worker. A view is
    handed a ``worker_name`` only when :attr:`addresses_workers`, and :meth:`cancel` a
    ``terminate=True`` only when :attr:`terminates`.
    """

    @property
    def addresses_workers(self) -> bool:
        """Whether the views can narrow to one worker by its name."""
        ...

    @property
    def terminates(self) -> bool:
        """Whether :meth:`cancel` serves ``terminate=True``: the backend's workers stop a task while it runs."""
        ...

    async def ping(self, worker_name: str | None) -> dict[str, Any]:
        """Ask the workers whether they are alive, and return their answers."""
        ...

    async def active_workers(self) -> list[str]:
        """The names of the workers alive now."""
        ...

    async def task_status(self, task_id: str) -> str:
        """The vendor's status word for the task ``task_id``."""
        ...

    async def task_result(self, task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
        """The task's ``(ready, status, value)``: whether it finished, its status word, its result.

        With a ``timeout`` it waits up to that many seconds for the task to finish; with ``None``
        it reads the current state at once. ``value`` is the result when ``ready``. A failed task
        raises its failure as the vendor retains it.
        """
        ...

    async def cancel(self, task_id: str, terminate: bool) -> str:
        """Cancel the task ``task_id``; return what was done.

        Without ``terminate`` a task that has not started is kept from running and a running one is
        left to finish; with ``terminate`` a running task is stopped as well. A backend that cannot
        cancel a running task without stopping it refuses the call without ``terminate`` for a
        running task rather than stopping it. The reply says what was done; when a worker answers
        that it cannot stop the task it runs, the call is refused rather than leaving the task
        running behind a reply that says otherwise, and a stop the workers did not confirm within
        the time the backend waits for it is reported as not confirmed, with the status the task
        reached.
        """
        ...

    async def active_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """The tasks running now."""
        ...

    async def reserved_tasks(self) -> dict[str, Any]:
        """The tasks taken off the queue and waiting for a free worker slot."""
        ...

    async def scheduled_tasks(self) -> dict[str, Any]:
        """The tasks waiting for a later time (an ``eta`` or ``countdown``)."""
        ...

    async def registered_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """The task names the workers can run."""
        ...

    async def failed_tasks(self) -> list[dict[str, Any]]:
        """The tasks that failed, as the vendor retains them."""
        ...

    async def worker_stats(self, worker_name: str | None) -> dict[str, Any]:
        """The workers' runtime statistics."""
        ...

    async def worker_queues(self, worker_name: str | None) -> dict[str, Any]:
        """The queues the workers consume."""
        ...
