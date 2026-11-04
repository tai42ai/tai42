"""What every schedule write door shares: the cadence check and the stored job kwargs.

Each door that writes a schedule (the schedule branch tool, update, import) checks the cadence
against the backend's :class:`~tai42_contract.backend.schedule_store.ScheduleWriter` before it
writes; the writer stores the record's tool inside the job kwargs it fires with.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.backend.schedule_store import ScheduleWriter
from tai42_contract.backend.scheduling import CrontabCadence, IntervalCadence, ScheduleRecord


def check_cadence_supported(writer: ScheduleWriter, cadence: IntervalCadence | CrontabCadence, label: str) -> None:
    """Raise ``ValueError`` when ``writer`` cannot honour ``cadence``; ``label`` names the backend."""
    if isinstance(cadence, IntervalCadence) and cadence.relative and not writer.relative_intervals:
        raise ValueError(f"the {label} backend does not support relative interval schedules (relative=True)")


def fired_kwargs(record: ScheduleRecord, tool_name_arg: str) -> dict[str, Any]:
    """The job kwargs ``record`` fires with: its kwargs plus its tool under ``tool_name_arg``."""
    return {**record.kwargs, tool_name_arg: record.tool}
