"""A fake scheduling backend for the schedule door tests: the contract's names and models, in memory.

It implements the protocol a real backend implements — the marker tools
(``backend_list_schedules`` / ``backend_delete_schedule``), the backup pair
(``backend_export_schedules`` / ``backend_import_schedules``) and, in the sibling
``schedule_ext`` module, the ``schedule_task`` extension — over one in-memory store. Each
stored job carries its fired tool under :data:`TOOL_NAME_KEY`, a key that is NOT the
platform's default tool-name argument, so the platform can only learn a schedule's tool
from the ``tool`` field the backend splits out.

``demo_report`` records every invocation, so a guard that must never reach the base tool is
proved by an empty record. ``plain_task`` has no schedule branch, so scheduling it on a
cadence raises the missing-extension error. ``list_rows_override`` makes the list tool return
whatever a test sets, so a malformed backend answer can be exercised.
"""

from typing import Any

from pydantic import TypeAdapter
from tai42_contract.app import tai42_app
from tai42_contract.backend import (
    CrontabCadence,
    IntervalCadence,
    ScheduleCadence,
    ScheduleImportError,
    ScheduleImportMode,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
)

#: The fake backend's own tool-name key inside a stored job's kwargs.
TOOL_NAME_KEY = "fake_target"

_CADENCE: TypeAdapter[IntervalCadence | CrontabCadence] = TypeAdapter(ScheduleCadence)

demo_calls: list[dict] = []
#: name -> {"args", "kwargs" (with TOOL_NAME_KEY), "cadence" (dumped), "enabled"}
store: dict[str, dict[str, Any]] = {}
list_rows_override: list[Any] | None = None


@tai42_app.tools.tool
def demo_report(payload: str = "hi") -> str:
    """Record the call and echo ``payload``."""
    demo_calls.append({"payload": payload})
    return payload


@tai42_app.tools.tool
def plain_task(payload: str = "hi") -> str:
    """A tool with no schedule branch attached."""
    return payload


@tai42_app.tools.tool
def backend_list_schedules() -> list:
    """The stored schedules as ``ScheduleListRow`` dumps (no args, no kwargs)."""
    if list_rows_override is not None:
        return list_rows_override
    return [
        ScheduleListRow(
            name=name,
            tool=entry["kwargs"][TOOL_NAME_KEY],
            cadence=_CADENCE.validate_python(entry["cadence"]),
            enabled=entry["enabled"],
        ).model_dump(mode="json")
        for name, entry in store.items()
    ]


@tai42_app.tools.tool
def backend_delete_schedule(name: str) -> dict:
    """Remove a stored schedule."""
    store.pop(name, None)
    return {"status": "deleted", "name": name}


@tai42_app.tools.tool
def backend_export_schedules() -> list:
    """Every stored schedule as a ``ScheduleRecord`` dump, the tool split out of its own key."""
    records = []
    for name, entry in store.items():
        kwargs = dict(entry["kwargs"])
        tool = kwargs.pop(TOOL_NAME_KEY)
        records.append(
            ScheduleRecord(
                name=name,
                tool=tool,
                args=entry["args"],
                kwargs=kwargs,
                cadence=_CADENCE.validate_python(entry["cadence"]),
                enabled=entry["enabled"],
            ).model_dump(mode="json")
        )
    return records


@tai42_app.tools.tool
def backend_import_schedules(schedules: list[dict], mode: ScheduleImportMode = "skip") -> dict:
    """Store each ``ScheduleRecord`` row, the tool injected under the backend's own key."""
    created = updated = skipped_existing = 0
    errors: list[ScheduleImportError] = []
    for index, row in enumerate(schedules):
        try:
            record = ScheduleRecord.model_validate(row)
        except ValueError as exc:
            errors.append(ScheduleImportError(index=index, name=row.get("name"), error=str(exc)))
            continue
        exists = record.name in store
        if exists and mode == "skip":
            skipped_existing += 1
            continue
        store[record.name] = {
            "args": record.args,
            "kwargs": {**record.kwargs, TOOL_NAME_KEY: record.tool},
            "cadence": record.cadence.model_dump(mode="json"),
            "enabled": record.enabled,
        }
        if exists:
            updated += 1
        else:
            created += 1
    return ScheduleImportReport(
        created=created, updated=updated, skipped=0, skipped_existing=skipped_existing, errors=errors
    ).model_dump()
