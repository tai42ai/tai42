"""The execution-backend launch lifecycle, implemented once for every backend.

A vendor binding declares its runtimes and implements
:class:`~tai42_contract.backend.runtime.BackendRuntime` — how ITS engine starts,
drains, and recycles. Everything host-shaped around that (subcommand dispatch,
boot-readiness gating, off-loop running, composed warm/cold shutdown wiring,
drain-on-cancellation, pool turnover after a registry-mutating fleet op, the
liveness file a progress runtime reports into) lives here, in
:class:`~tai42_kit.backend.base.ManagedBackend`, beside the asyncio future
chaining a process started through a process prelude binds
(:mod:`tai42_kit.backend.reshaped_threads`). The contract's scheduling protocol
(names and models) is re-exported here beside the kit's dispatch helpers and the tools every
backend registers over its seams: the dispatching branches (:mod:`tai42_kit.backend.branches`),
the schedule tools (:mod:`tai42_kit.backend.schedule_tools`) and the task and worker tools
(:mod:`tai42_kit.backend.inspection_tools`), whose names and refusal
:mod:`tai42_kit.backend.tool_surface` holds.

It lives in kit and not in the skeleton because a backend plugin may not import
the skeleton, and not in the contract because the contract carries no logic.
Kit is the only package both a plugin and the skeleton may import.

This subpackage is the one part of kit that reaches the running app: it imports
``tai42_contract.app.tai42_app`` for the readiness latch and the fleet-op hook.
That stays inside the leaf rule (the contract is kit's only tai dependency and
the handle is dependency-free), and ``tai42_kit`` re-exports nothing, so
``import tai42_kit`` still pulls nothing new.
"""

from tai42_contract.backend.scheduling import (
    SCHEDULE_BRANCH_SUFFIX,
    SCHEDULE_CADENCE_ADAPTER,
    SCHEDULE_CADENCE_KEY,
    SCHEDULE_DELETE_TOOL,
    SCHEDULE_EXPORT_TOOL,
    SCHEDULE_EXTENSION,
    SCHEDULE_IMPORT_TOOL,
    SCHEDULE_LIST_TOOL,
    SCHEDULE_MARKER_TOOLS,
    SCHEDULE_NAME_KEY,
    CrontabCadence,
    IntervalCadence,
    ScheduleCadence,
    ScheduleCreate,
    ScheduleDetail,
    ScheduleImportError,
    ScheduleImportMode,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
    ScheduleRowError,
)

from tai42_kit.backend.base import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    DEFAULT_READY_TIMEOUT_SECONDS,
    DRAIN_MARGIN_SECONDS,
    ManagedBackend,
)
from tai42_kit.backend.branches import ASYNC_TASK_EXTENSION, SYNC_TASK_EXTENSION, register_task_branches
from tai42_kit.backend.callback import (
    CallbackSchema,
    callback_execution,
    carry_forwarded_fire,
    prepare_backend_kwargs,
)
from tai42_kit.backend.conformance import check_backend_declarations, check_runtime_declarations, check_tool_surface
from tai42_kit.backend.inspection_tools import BackendInspection, register_inspection_tools
from tai42_kit.backend.private_loop import PrivateLoop
from tai42_kit.backend.queue_labels import QUEUE_LABEL_PATTERN, check_queue_label, derived_queue, queue_label_option
from tai42_kit.backend.reshaped_threads import bind_future_chaining, future_chaining_bound
from tai42_kit.backend.schedule_fire import backend_fire, fire_schedule_door
from tai42_kit.backend.schedule_tools import NO_CHANGES, register_schedule_tools
from tai42_kit.backend.schedule_writes import check_cadence_supported, fired_kwargs
from tai42_kit.backend.settings import BackendDispatchSettings
from tai42_kit.backend.shared_loop import SharedLoop
from tai42_kit.backend.task_options import (
    BACKEND_TASK_OPTS,
    CALLBACK_OPTION,
    SCHEDULE_OPTS,
    split_task_options,
    take_callback,
)
from tai42_kit.backend.tool_surface import (
    BACKEND_TOOL_TAG,
    BACKEND_TOOLS,
    INSPECTION_TOOLS,
    NOT_READY,
    SCHEDULE_TOOLS,
    UnsupportedBackendTool,
)

__all__ = [
    "ASYNC_TASK_EXTENSION",
    "BACKEND_TASK_OPTS",
    "BACKEND_TOOLS",
    "BACKEND_TOOL_TAG",
    "CALLBACK_OPTION",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_READY_TIMEOUT_SECONDS",
    "DRAIN_MARGIN_SECONDS",
    "INSPECTION_TOOLS",
    "NOT_READY",
    "NO_CHANGES",
    "QUEUE_LABEL_PATTERN",
    "SCHEDULE_BRANCH_SUFFIX",
    "SCHEDULE_CADENCE_ADAPTER",
    "SCHEDULE_CADENCE_KEY",
    "SCHEDULE_DELETE_TOOL",
    "SCHEDULE_EXPORT_TOOL",
    "SCHEDULE_EXTENSION",
    "SCHEDULE_IMPORT_TOOL",
    "SCHEDULE_LIST_TOOL",
    "SCHEDULE_MARKER_TOOLS",
    "SCHEDULE_NAME_KEY",
    "SCHEDULE_OPTS",
    "SCHEDULE_TOOLS",
    "SYNC_TASK_EXTENSION",
    "BackendDispatchSettings",
    "BackendInspection",
    "CallbackSchema",
    "CrontabCadence",
    "IntervalCadence",
    "ManagedBackend",
    "PrivateLoop",
    "ScheduleCadence",
    "ScheduleCreate",
    "ScheduleDetail",
    "ScheduleImportError",
    "ScheduleImportMode",
    "ScheduleImportReport",
    "ScheduleListRow",
    "ScheduleRecord",
    "ScheduleRowError",
    "SharedLoop",
    "UnsupportedBackendTool",
    "backend_fire",
    "bind_future_chaining",
    "callback_execution",
    "carry_forwarded_fire",
    "check_backend_declarations",
    "check_cadence_supported",
    "check_queue_label",
    "check_runtime_declarations",
    "check_tool_surface",
    "derived_queue",
    "fire_schedule_door",
    "fired_kwargs",
    "future_chaining_bound",
    "prepare_backend_kwargs",
    "queue_label_option",
    "register_inspection_tools",
    "register_schedule_tools",
    "register_task_branches",
    "split_task_options",
    "take_callback",
]
