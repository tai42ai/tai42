"""The rq queue every enqueue site and the scheduler use: its default job timeout is ``task_timeout``."""

from __future__ import annotations

from typing import Any

from rq import Queue

from tai42_backend_rq.settings import rq_settings

#: The import path ``rq-scheduler`` resolves its ``--queue-class`` from.
TASK_BUDGET_QUEUE_PATH = "tai42_backend_rq.queue.TaskBudgetQueue"


class TaskBudgetQueue(Queue):
    """An rq queue whose default job timeout is the dispatch's ``task_timeout``.

    rq gives a job the queue's default timeout when the job is created on it and when a
    scheduled job without one is moved onto it, so every job — enqueued or scheduler-fired —
    is bounded by the same budget.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Construct the queue with ``task_timeout`` as its default job timeout unless one is given."""
        kwargs.setdefault("default_timeout", rq_settings().task_timeout)
        super().__init__(*args, **kwargs)
