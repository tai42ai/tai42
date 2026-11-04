"""Execution-backend contract.

Exposes the ``Backend`` ABC, the ``BackendRuntime`` ABC one launch subcommand implements with
its liveness declarations, the ``CallbackSchema``, the scheduling protocol's names and models, and
the three seams a backend plugs its engine into: the ``TaskDoor`` (its queue), the ``ScheduleStore``
(its schedule storage, read back as ``StoredSchedule``; its ``ScheduleWriter`` part is the one
schedule writer) and the ``WorkerInspection`` (its worker views).
"""

from __future__ import annotations

from tai42_contract.backend.base import Backend
from tai42_contract.backend.callback import CallbackSchema
from tai42_contract.backend.dispatch import TaskDoor, TaskWaitTimeoutError
from tai42_contract.backend.inspection import WorkerInspection
from tai42_contract.backend.runtime import (
    CONSUMING_RUNTIME,
    PROCESS_PRELUDE_ENV,
    BackendRuntime,
    ExecutionMode,
    FleetOpApplied,
    FleetOpEffect,
    HttpLiveness,
    Liveness,
    ProcessPreludeRequired,
    ProgressLiveness,
    ProgressReporter,
)
from tai42_contract.backend.schedule_store import ScheduleStore, ScheduleWriter, StoredSchedule
from tai42_contract.backend.scheduling import (
    CRONTAB_FIELDS,
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

__all__ = [
    "CONSUMING_RUNTIME",
    "CRONTAB_FIELDS",
    "PROCESS_PRELUDE_ENV",
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
    "Backend",
    "BackendRuntime",
    "CallbackSchema",
    "CrontabCadence",
    "ExecutionMode",
    "FleetOpApplied",
    "FleetOpEffect",
    "HttpLiveness",
    "IntervalCadence",
    "Liveness",
    "ProcessPreludeRequired",
    "ProgressLiveness",
    "ProgressReporter",
    "ScheduleCadence",
    "ScheduleCreate",
    "ScheduleDetail",
    "ScheduleImportError",
    "ScheduleImportMode",
    "ScheduleImportReport",
    "ScheduleListRow",
    "ScheduleRecord",
    "ScheduleRowError",
    "ScheduleStore",
    "ScheduleWriter",
    "StoredSchedule",
    "TaskDoor",
    "TaskWaitTimeoutError",
    "WorkerInspection",
]
