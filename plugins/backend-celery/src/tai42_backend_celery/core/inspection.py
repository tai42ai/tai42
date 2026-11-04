"""The Celery worker inspection: Celery's control ``inspect`` API and ``AsyncResult``.

Each worker view broadcasts one ``inspect`` call and replies Celery's own shape, a mapping of
worker name to that worker's answer; with no worker answering it is ``{}``. A cancel of a task
that has not ended broadcasts one revoke and replies from the workers' answers to it and the
task's state. Every broadcast waits for answers for :data:`REPLY_SECONDS`. The calls block, so
each runs on a thread. Celery keeps no queryable index of failed tasks, so ``failed_tasks`` keeps
the base's refusal.
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery.app.control import Inspect
from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.result import AsyncResult
from tai42_kit.backend import BackendInspection, UnsupportedBackendTool

from tai42_backend_celery.core.app import celery_app

#: How long a broadcast to the workers waits for their answers: Celery's own default for ``inspect``.
REPLY_SECONDS = 1.0

# The states in which a task has ended. REVOKED is not one: a worker stores it the moment it hears
# a revoke, while the task may still run or still be reserved.
_ENDED = frozenset({"SUCCESS", "FAILURE"})


def _inspect(worker_name: str | None) -> Inspect:
    return celery_app.control.inspect([worker_name] if worker_name is not None else None, timeout=REPLY_SECONDS)


def _state(task_id: str) -> str:
    """The result backend's state of the task, read afresh.

    An AsyncResult caches a ready state, and REVOKED is ready while the task may still run.
    """
    return AsyncResult(task_id, app=celery_app).state


def _revoke(label: str, task_id: str, terminate: bool) -> str:
    """Revoke ``task_id`` unless it has ended, awaiting the workers' answers, and reply what they did.

    Each answering worker says it flagged the task revoked, ended it (``terminate: <id>``), runs
    no such task (``terminate: tasks unknown``), or could not end it (an ``error``, from a pool
    that cannot end a running task), which is raised as a refusal naming the backend by its
    ``label``. The answers come from the workers that heard the revoke within
    :data:`REPLY_SECONDS`, never from all workers, so a stop is confirmed only by the worker that
    ended the task; otherwise the task's state says whether it ended meanwhile or the stop is not
    confirmed.
    """
    state = _state(task_id)
    if state in _ENDED:
        return f"Task {task_id} cannot be canceled (status: {state})"
    answers = [
        (worker, answer)
        for replies in celery_app.control.revoke(task_id, terminate=terminate, reply=True, timeout=REPLY_SECONDS)
        for worker, answer in replies.items()
    ]
    for worker, answer in answers:
        if "error" in answer:
            raise UnsupportedBackendTool(
                label,
                f"backend_cancel_task with terminate on worker {worker!r}, "
                f"whose pool cannot end a running task: {answer['error']}; "
                "the task is revoked and runs to its end",
            )
    if not terminate:
        if answers:
            return f"Task {task_id} revoked"
        return f"Task {task_id} revoke requested but not confirmed (status: {_state(task_id)})"
    if any(answer.get("ok") == f"terminate: {task_id}" for _worker, answer in answers):
        return f"Task {task_id} revoked and stopped"
    state = _state(task_id)
    if state in _ENDED:
        return f"Task {task_id} cannot be stopped (status: {state})"
    return f"Task {task_id} stop requested but not confirmed (status: {state})"


def _read_result(task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
    result = AsyncResult(task_id, app=celery_app)
    if timeout is None:
        if not result.ready():
            return False, result.status, None
        return True, result.status, result.get()
    try:
        value = result.get(timeout=timeout)
    except CeleryTimeoutError:
        return False, result.status, None
    return True, result.status, value


class CeleryInspection(BackendInspection):
    """Celery's views of its workers and their tasks, each worker addressable by its name."""

    @property
    def addresses_workers(self) -> bool:
        """``True``: an ``inspect`` call narrows to the named worker."""
        return True

    @property
    def terminates(self) -> bool:
        """``True``: a revoke with terminate ends the running task on every pool whose worker can end a job.

        A worker that answers that it cannot stop the task refuses it in its reply, which the tool raises.
        """
        return True

    async def ping(self, worker_name: str | None) -> dict[str, Any]:
        """Each answering worker's pong."""
        return await asyncio.to_thread(lambda: _inspect(worker_name).ping() or {})

    async def active_workers(self) -> list[str]:
        """The names of the workers that answer a ping."""
        return list(await self.ping(None))

    async def task_status(self, task_id: str) -> str:
        """The result backend's state of the task (``PENDING`` for a task it does not know)."""
        return await asyncio.to_thread(_state, task_id)

    async def task_result(self, task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
        """The task's ``(ready, status, value)``; a failed task raises its failure."""
        return await asyncio.to_thread(_read_result, task_id, timeout)

    async def cancel(self, task_id: str, terminate: bool) -> str:
        """Revoke the task, with ``terminate`` ending it where it runs, and reply what the workers did.

        A task that has ended is reported with its state and no revoke is sent. A worker whose
        pool cannot end a running task refuses the stop, and the call raises
        ``UnsupportedBackendTool``; a stop no worker confirms within :data:`REPLY_SECONDS` is
        reported as not confirmed, with the task's state, which reads ``REVOKED`` from the moment
        any worker has heard the revoke.
        """
        return await asyncio.to_thread(_revoke, self.label, task_id, terminate)

    async def active_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """Each worker's running tasks."""
        return await asyncio.to_thread(lambda: _inspect(worker_name).active() or {})

    async def reserved_tasks(self) -> dict[str, Any]:
        """Each worker's prefetched tasks, not yet started."""
        return await asyncio.to_thread(lambda: _inspect(None).reserved() or {})

    async def scheduled_tasks(self) -> dict[str, Any]:
        """Each worker's tasks held for their ``eta``, each with its request."""
        return await asyncio.to_thread(lambda: _inspect(None).scheduled() or {})

    async def registered_tasks(self, worker_name: str | None) -> dict[str, Any]:
        """Each worker's registered task names."""
        return await asyncio.to_thread(lambda: _inspect(worker_name).registered() or {})

    async def worker_stats(self, worker_name: str | None) -> dict[str, Any]:
        """Each worker's stats: pool, totals, broker and process info."""
        return await asyncio.to_thread(lambda: _inspect(worker_name).stats() or {})

    async def worker_queues(self, worker_name: str | None) -> dict[str, Any]:
        """Each worker's consumed queues."""
        return await asyncio.to_thread(lambda: _inspect(worker_name).active_queues() or {})
