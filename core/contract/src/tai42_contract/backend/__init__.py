"""Execution-backend contract.

Exposes the ``Backend`` ABC, the ``BackendRuntime`` ABC one launch subcommand implements with
its liveness declarations, the ``CallbackSchema``, and the scheduling protocol's names and models.
"""

from __future__ import annotations

from tai42_contract.backend.base import Backend
from tai42_contract.backend.callback import CallbackSchema
from tai42_contract.backend.runtime import (
    CONSUMING_RUNTIME,
    BackendRuntime,
    ExecutionMode,
    FleetOpApplied,
    FleetOpEffect,
    HttpLiveness,
    Liveness,
    ProgressLiveness,
    ProgressReporter,
)
from tai42_contract.backend.scheduling import (
    SCHEDULE_BRANCH_SUFFIX,
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
    ScheduleImportError,
    ScheduleImportMode,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
    ScheduleRowError,
)

__all__ = [
    "CONSUMING_RUNTIME",
    "SCHEDULE_BRANCH_SUFFIX",
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
    "ProgressLiveness",
    "ProgressReporter",
    "ScheduleCadence",
    "ScheduleCreate",
    "ScheduleImportError",
    "ScheduleImportMode",
    "ScheduleImportReport",
    "ScheduleListRow",
    "ScheduleRecord",
    "ScheduleRowError",
]
