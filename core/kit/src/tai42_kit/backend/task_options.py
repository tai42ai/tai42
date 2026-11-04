"""The options a dispatching branch tool takes beyond the tool's own arguments, and their split.

:data:`BACKEND_TASK_OPTS` are the task options every backend honours; a backend adds its own
through :attr:`~tai42_contract.backend.dispatch.TaskDoor.task_opts`. :data:`SCHEDULE_OPTS` are the
schedule branch's own arguments. Each becomes a keyword argument of the branch tool, so a
dispatch's kwargs carry them beside the tool's arguments until :func:`split_task_options` takes
them out.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tai42_contract.backend.scheduling import SCHEDULE_CADENCE_KEY, SCHEDULE_NAME_KEY

from tai42_kit.backend.callback import CallbackSchema, carry_forwarded_fire

#: The task option naming the follow-up tool chained onto a job's result.
CALLBACK_OPTION = "callback_kwargs"

#: The task options of the sync and async branch tools every backend honours, by name and annotation.
BACKEND_TASK_OPTS: dict[str, Any] = {
    "countdown": int | None,  # Seconds to wait before the job may run.
    "expires": str | float | None,  # Seconds the queued job stays runnable.
    "eta": str | None,  # ISO-format datetime the job may run from.
    CALLBACK_OPTION: CallbackSchema | None,
    "queue": str | None,  # Worker queue label (tai42_kit.backend.queue_labels); None = the settings queue.
}

#: The schedule branch tool's own arguments: the schedule's name and its cadence.
SCHEDULE_OPTS: dict[str, Any] = {
    SCHEDULE_NAME_KEY: str,
    SCHEDULE_CADENCE_KEY: dict[str, Any],
}


def split_task_options(kwargs: dict[str, Any], opts: Iterable[str]) -> dict[str, Any]:
    """Pop every option named in ``opts`` out of ``kwargs``; return those the caller gave a value.

    An option whose value is ``None`` was not given and is dropped; every other value, a falsy
    one included, is kept.
    """
    taken = {key: kwargs.pop(key) for key in opts if key in kwargs}
    return {key: value for key, value in taken.items() if value is not None}


def take_callback(options: dict[str, Any], kwargs: dict[str, Any]) -> CallbackSchema | None:
    """Pop the callback out of the split ``options``; carry onto it what its follow-up job needs.

    A callback given as a plain mapping is validated into a :class:`CallbackSchema` (an invalid
    one raises ``ValidationError``). ``kwargs`` are the job's prepared kwargs: the forwarded door
    pair the follow-up job needs is read from them
    (:func:`~tai42_kit.backend.callback.carry_forwarded_fire`).
    """
    raw = options.pop(CALLBACK_OPTION, None)
    if raw is None:
        return None
    callback = raw if isinstance(raw, CallbackSchema) else CallbackSchema.model_validate(raw)
    carry_forwarded_fire(callback, kwargs)
    return callback
