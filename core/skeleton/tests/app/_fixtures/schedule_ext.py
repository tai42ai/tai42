"""The fake scheduling backend's ``schedule_task`` extension (see ``schedule_tools``).

Branches ``<name>_schedule_task`` presenting the base tool's parameters plus the contract's
schedule keys (``SCHEDULE_NAME_KEY`` / ``SCHEDULE_CADENCE_KEY``) and the reserved keys the
create door stamps, then validates the cadence as a ``ScheduleCadence`` and stores the job in
the fake backend's store under its own tool-name key — no queue, no Redis. Mirrors a real
backend branch's signature so the REAL ``run_tool`` binding validates the dispatched arguments.
Every branch call is also captured in :data:`captured_schedules`.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from typing import Any

from makefun import create_function
from pydantic import TypeAdapter
from tai42_contract.app import tai42_app
from tai42_contract.backend import (
    SCHEDULE_BRANCH_SUFFIX,
    SCHEDULE_CADENCE_KEY,
    SCHEDULE_EXTENSION,
    SCHEDULE_NAME_KEY,
    CrontabCadence,
    IntervalCadence,
    ScheduleCadence,
)
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.schedule_subject import SCHEDULE_STAMPED_DOOR_OPTS, SCHEDULE_STATE_BINDING_REQUEST_KEY

_BACKEND_MODULE = "tests.app._fixtures.schedule_tools"
_CADENCE: TypeAdapter[IntervalCadence | CrontabCadence] = TypeAdapter(ScheduleCadence)

captured_schedules: list[dict] = []

_SCHEDULE_OPTS: dict[str, Any] = {
    SCHEDULE_NAME_KEY: str,
    SCHEDULE_CADENCE_KEY: dict[str, Any],
    SCHEDULE_STATE_BINDING_REQUEST_KEY: dict[str, Any] | None,
    **SCHEDULE_STAMPED_DOOR_OPTS,
}


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name=SCHEDULE_EXTENSION)
def schedule_task(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_schedule_task``, storing the schedule in the fake backend."""
    raw_name = f"{name}{SCHEDULE_BRANCH_SUFFIX}"
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
        backend = importlib.import_module(_BACKEND_MODULE)
        schedule_name = kwargs.pop(SCHEDULE_NAME_KEY)
        cadence = _CADENCE.validate_python(kwargs.pop(SCHEDULE_CADENCE_KEY))
        stored = {key: value for key, value in kwargs.items() if value is not None}
        captured_schedules.append({"name": schedule_name, "cadence": cadence, "kwargs": dict(stored)})
        backend.store[schedule_name] = {
            "args": list(args),
            "kwargs": {**stored, backend.TOOL_NAME_KEY: name},
            "cadence": cadence.model_dump(mode="json"),
            "enabled": True,
        }
        return {"scheduled": schedule_name}

    branch = create_function(
        func_signature=sig,
        func_impl=func_impl,
        func_name=makefun_func_name(raw_name),
        module_name=func.__module__,
        doc=description,
    )
    branch.__name__ = raw_name
    return branch
