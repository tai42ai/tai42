"""A ``schedule_task`` BACKEND extension for the schedule-create translation test.

Branches ``<name>_schedule_task`` presenting the base tool's parameters plus the
two expert schedule keys (``backend_schedule_name`` / ``backend_schedule``), then
CAPTURES whatever schedule the create door translated onto it — no queue, no Redis.
Mirrors the real backend branch's signature so the REAL ``run_tool`` binding
validates the dispatched arguments against it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.data import makefun_func_name

captured_schedules: list[dict] = []

_SCHEDULE_OPTS: dict[str, Any] = {
    "backend_schedule_name": str,
    "backend_schedule": int | float | str | dict[str, Any],
}


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="schedule_task")
def schedule_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_schedule_task``, capturing the schedule."""
    raw_name = f"{name}_schedule_task"
    params = list(inspect.signature(func).parameters.values())
    opts = [
        inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=annotation)
        for key, annotation in _SCHEDULE_OPTS.items()
    ]
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        *head, var_keyword = params
        new_params = [*head, *opts, var_keyword]
    else:
        new_params = params + opts
    sig = inspect.Signature(parameters=new_params, return_annotation=inspect.Signature.empty)

    def func_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        schedule_name = kwargs.pop("backend_schedule_name", None)
        backend_schedule = kwargs.pop("backend_schedule", None)
        captured_schedules.append({"name": schedule_name, "backend_schedule": backend_schedule, "kwargs": dict(kwargs)})
        return {"scheduled": schedule_name, "backend_schedule": backend_schedule}

    branch = create_function(
        func_signature=sig,
        func_impl=func_impl,
        func_name=makefun_func_name(raw_name),
        module_name=func.__module__,
        doc=description,
    )
    branch.__name__ = raw_name
    return branch
