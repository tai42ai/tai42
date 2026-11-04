"""The twelve task and worker tools every backend offers, built once over its worker inspection.

:class:`BackendInspection` is the base a backend's
:class:`~tai42_contract.backend.inspection.WorkerInspection` builds on: each view it does not
override refuses with :class:`~tai42_kit.backend.tool_surface.UnsupportedBackendTool`, and its
capabilities default to ``False``. :func:`register_inspection_tools` registers the tools over it,
each with one argument list on every backend; a ``worker_name`` the backend cannot address, or a
``terminate`` it cannot honour, is refused here before the view is reached.
"""

from typing import Any

from tai42_kit.backend.job_context import assert_not_in_job
from tai42_kit.backend.tool_surface import NOT_READY, UnsupportedBackendTool, backend_tool


class BackendInspection:
    """A backend's worker inspection: every view refused until the backend implements it.

    ``label`` is the backend's name, as a refusal names it. A subclass overrides the views its
    engine answers and sets :attr:`addresses_workers` / :attr:`terminates` when it can.
    """

    def __init__(self, label: str) -> None:
        """Name the backend by its ``label``."""
        self.label = label

    @property
    def addresses_workers(self) -> bool:
        """Whether the views can narrow to one worker by its name; ``False`` unless overridden."""
        return False

    @property
    def terminates(self) -> bool:
        """Whether :meth:`cancel` serves ``terminate=True``; ``False`` unless overridden."""
        return False

    def _unsupported(self, tool: str) -> UnsupportedBackendTool:
        return UnsupportedBackendTool(self.label, tool)

    async def ping(self, worker_name: str | None) -> dict[str, Any]:
        """Ask the workers whether they are alive, and return their answers."""
        raise self._unsupported("backend_ping_worker")

    async def active_workers(self) -> list[str]:
        """The names of the workers alive now."""
        raise self._unsupported("backend_list_active_workers")

    async def task_status(self, task_id: str) -> str:
        """The vendor's status word for the task ``task_id``."""
        raise self._unsupported("backend_task_status")

    async def task_result(self, task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
        """The task's ``(ready, status, value)``."""
        raise self._unsupported("backend_task_result")

    async def cancel(self, task_id: str, terminate: bool) -> str:
        """Cancel the task ``task_id``; return what was done."""
        raise self._unsupported("backend_cancel_task")

    async def active_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """The tasks running now."""
        raise self._unsupported("backend_active_tasks")

    async def reserved_tasks(self) -> dict[str, Any]:
        """The tasks taken off the queue and waiting for a free worker slot."""
        raise self._unsupported("backend_reserved_tasks")

    async def scheduled_tasks(self) -> dict[str, Any]:
        """The tasks waiting for a later time."""
        raise self._unsupported("backend_scheduled_tasks")

    async def registered_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """The task names the workers can run."""
        raise self._unsupported("backend_registered_tasks")

    async def failed_tasks(self) -> list[dict[str, Any]]:
        """The tasks that failed."""
        raise self._unsupported("backend_list_failed_tasks")

    async def worker_stats(self, worker_name: str | None) -> dict[str, Any]:
        """The workers' runtime statistics."""
        raise self._unsupported("backend_worker_stats")

    async def worker_queues(self, worker_name: str | None) -> dict[str, Any]:
        """The queues the workers consume."""
        raise self._unsupported("backend_worker_queues")


def register_inspection_tools(inspection: BackendInspection, module: str) -> None:  # noqa: C901 - one closure per tool
    """Register the twelve task and worker tools of one backend over ``inspection``.

    ``module`` is the backend package the tools are registered as tools of
    (:func:`~tai42_kit.backend.tool_surface.backend_tool`).
    """
    tool = backend_tool(module)

    def addressed(tool_name: str, worker_name: str | None) -> str | None:
        if worker_name is not None and not inspection.addresses_workers:
            raise UnsupportedBackendTool(inspection.label, f"{tool_name} with a worker_name")
        return worker_name

    @tool
    async def backend_ping_worker(worker_name: str | None = None) -> dict[str, Any]:
        """Ping every worker, or the one named, and reply the answers of those alive."""
        return await inspection.ping(addressed("backend_ping_worker", worker_name))

    @tool
    async def backend_list_active_workers() -> list[str]:
        """List the names of the workers alive now."""
        return await inspection.active_workers()

    @tool
    async def backend_task_status(task_id: str) -> str:
        """Reply the backend's status word for a task."""
        return await inspection.task_status(task_id)

    @tool
    async def backend_task_result(task_id: str, timeout: float | None = None) -> Any:
        """Reply the result of a finished task.

        With a ``timeout``, wait up to that many seconds for the task to finish; with none, read
        its state at once. A task still unfinished replies ``Task <id> is not ready (status:
        <status>)``; a failed task raises its failure.

        A job does not wait for another job's result: given a ``timeout`` inside a job, it raises
        ``RuntimeError`` (a job chains the next one with ``callback_kwargs``).
        """
        if timeout is not None:
            assert_not_in_job("backend_task_result")
        ready, state, value = await inspection.task_result(task_id, timeout)
        if not ready:
            return NOT_READY.format(task_id=task_id, status=state)
        return value

    @tool
    async def backend_cancel_task(task_id: str, terminate: bool = False) -> str:
        """Cancel a task: keep a task that has not started from running; with ``terminate`` also stop it while it runs.

        A backend that does not serve ``terminate=True`` refuses it; one that cannot cancel a
        running task without stopping it refuses ``terminate=False`` for a running task; a worker
        that answers that it cannot stop the task it runs refuses ``terminate=True`` for that task.
        Each refusal raises ``UnsupportedBackendTool``. The reply says what was done, or that the
        stop was requested but not confirmed.
        """
        if terminate and not inspection.terminates:
            raise UnsupportedBackendTool(inspection.label, "backend_cancel_task with terminate")
        return await inspection.cancel(task_id, terminate)

    @tool
    async def backend_active_tasks(worker_name: str | None = None) -> dict[str, Any]:
        """List the tasks running now, on every worker or the one named."""
        return await inspection.active_tasks(addressed("backend_active_tasks", worker_name))

    @tool
    async def backend_reserved_tasks() -> dict[str, Any]:
        """List the tasks taken off the queue and waiting for a free worker slot."""
        return await inspection.reserved_tasks()

    @tool
    async def backend_scheduled_tasks() -> dict[str, Any]:
        """List the tasks waiting for a later time (an ``eta`` or ``countdown``)."""
        return await inspection.scheduled_tasks()

    @tool
    async def backend_registered_tasks(worker_name: str | None = None) -> dict[str, Any]:
        """List the task names the workers can run, on every worker or the one named."""
        return await inspection.registered_tasks(addressed("backend_registered_tasks", worker_name))

    @tool
    async def backend_list_failed_tasks() -> list[dict[str, Any]]:
        """List the tasks that failed, as the backend retains them."""
        return await inspection.failed_tasks()

    @tool
    async def backend_worker_stats(worker_name: str | None = None) -> dict[str, Any]:
        """Reply the workers' runtime statistics, for every worker or the one named."""
        return await inspection.worker_stats(addressed("backend_worker_stats", worker_name))

    @tool
    async def backend_worker_queues(worker_name: str | None = None) -> dict[str, Any]:
        """List the queues the workers consume, for every worker or the one named."""
        return await inspection.worker_queues(addressed("backend_worker_queues", worker_name))
