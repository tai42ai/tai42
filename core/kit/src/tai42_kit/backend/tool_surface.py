"""The ``backend_*`` tool surface every backend offers: its names, its refusal and its not-ready text.

The kit registers the surface once over a backend's seams: the ten schedule tools over its
:class:`~tai42_contract.backend.schedule_store.ScheduleStore`
(:mod:`tai42_kit.backend.schedule_tools`) and the twelve task and worker tools over its
:class:`~tai42_contract.backend.inspection.WorkerInspection`
(:mod:`tai42_kit.backend.inspection_tools`). A tool the backend cannot serve is still
registered, and raises :class:`UnsupportedBackendTool` when called.

Each tool is registered through :func:`backend_tool` as a tool of the backend package that
registers the surface, not of the kit module that builds it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, TypeVar

from tai42_contract.app import tai42_app
from tai42_contract.backend.scheduling import (
    SCHEDULE_DELETE_TOOL,
    SCHEDULE_EXPORT_TOOL,
    SCHEDULE_IMPORT_TOOL,
    SCHEDULE_LIST_TOOL,
)

#: The tag every ``backend_*`` tool is registered under.
BACKEND_TOOL_TAG: Final = "backend"

#: The schedule tools, registered over the backend's schedule store.
SCHEDULE_TOOLS: Final = (
    SCHEDULE_LIST_TOOL,
    "backend_get_schedule",
    "backend_update_schedule",
    "backend_enable_schedule",
    "backend_disable_schedule",
    "backend_run_schedule_now",
    "backend_schedule_exists",
    SCHEDULE_DELETE_TOOL,
    SCHEDULE_EXPORT_TOOL,
    SCHEDULE_IMPORT_TOOL,
)

#: The task and worker tools, registered over the backend's worker inspection.
INSPECTION_TOOLS: Final = (
    "backend_ping_worker",
    "backend_list_active_workers",
    "backend_task_status",
    "backend_task_result",
    "backend_cancel_task",
    "backend_active_tasks",
    "backend_reserved_tasks",
    "backend_scheduled_tasks",
    "backend_registered_tasks",
    "backend_list_failed_tasks",
    "backend_worker_stats",
    "backend_worker_queues",
)

#: Every ``backend_*`` tool a backend registers, and no other.
BACKEND_TOOLS: Final = SCHEDULE_TOOLS + INSPECTION_TOOLS

_Tool = TypeVar("_Tool", bound=Callable[..., Any])


def backend_tool(module: str) -> Callable[[_Tool], _Tool]:
    """The decorator registering one ``backend_*`` tool, tagged ``backend``, as a tool of ``module``.

    ``module`` is the backend package registering the surface. The host attributes a tool to the
    module its function names (``__module__``) and admits the tools of the package the manifest's
    ``backend_module`` names, so the kit-built function is named into that package before it is
    registered. The builder modules keep their annotations evaluated, so a tool's annotations
    resolve whichever module it names.
    """

    def register(func: _Tool) -> _Tool:
        func.__module__ = module
        return tai42_app.tools.tool(tags={BACKEND_TOOL_TAG})(func)

    return register


#: The reply of a task read whose task has not finished.
NOT_READY: Final = "Task {task_id} is not ready (status: {status})"


class UnsupportedBackendTool(NotImplementedError):  # noqa: N818 - named as its base is: a tool not implemented
    """Raised by a ``backend_*`` tool, or one form of it, the backend cannot serve."""

    def __init__(self, label: str, tool: str) -> None:
        """Name the backend by its ``label`` and the ``tool`` (or form of it) it does not serve."""
        super().__init__(f"backend {label!r} does not support {tool}")
        self.label = label
        self.tool = tool
