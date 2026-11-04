"""The ten schedule tools every scheduling backend offers, built once over its schedule store.

:func:`register_schedule_tools` registers them over a backend's
:class:`~tai42_contract.backend.schedule_store.ScheduleStore`: list, get, update, enable, disable,
run now, exists, delete, and the export/import pair. Each tool has one argument list and one
reply on every backend; a status reply is ``{"status": ..., "name": name}``. What a store cannot
represent is refused here, by its declared capabilities: a disabled schedule on a store that holds
none, a relative interval on a store that honours none.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tai42_contract.backend.schedule_store import ScheduleStore, StoredSchedule
from tai42_contract.backend.scheduling import (
    ScheduleDetail,
    ScheduleImportError,
    ScheduleImportMode,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
)

from tai42_kit.backend.schedule_writes import check_cadence_supported
from tai42_kit.backend.settings import BackendDispatchSettings
from tai42_kit.backend.tool_surface import UnsupportedBackendTool, backend_tool
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

#: The message of an update given no change.
NO_CHANGES = "No changes provided; nothing updated."


def split_fired_tool(name: str, kwargs: dict[str, Any], tool_name_arg: str) -> tuple[str, dict[str, Any]]:
    """The fired tool's name and the remaining kwargs of a stored schedule's job kwargs.

    ``kwargs`` is left untouched. A schedule without ``tool_name_arg`` in its kwargs was not
    created through the backend's schedule door, and raises ``ValueError``.
    """
    if tool_name_arg not in kwargs:
        raise ValueError(
            f"schedule {name!r} carries no {tool_name_arg!r}; it was not created through this backend's schedule door"
        )
    rest = {key: value for key, value in kwargs.items() if key != tool_name_arg}
    return kwargs[tool_name_arg], rest


def disabled_import_refusal(name: str, label: str) -> str:
    """The import error of a disabled record on a store that holds no disabled schedule."""
    return (
        f"schedule {name!r} has enabled=False; the {label} backend has no disabled-schedule "
        "representation, so it was not created as an active schedule"
    )


def register_schedule_tools(  # noqa: C901, PLR0915 - one closure per tool
    store: ScheduleStore,
    settings: Callable[[], BackendDispatchSettings],
    label: str,
    module: str,
) -> None:
    """Register the ten schedule tools of one backend over ``store``.

    ``settings`` is the backend's settings accessor, read at each call for the tool-name argument
    the stored job kwargs carry the fired tool under. ``label`` is the backend's name, as a
    refusal names it. ``module`` is the backend package the tools are registered as tools of
    (:func:`~tai42_kit.backend.tool_surface.backend_tool`).
    """
    tool = backend_tool(module)

    def status(state: str, name: str) -> dict[str, Any]:
        return {"status": state, "name": name}

    def listing_row(stored: StoredSchedule) -> ScheduleListRow:
        fired_tool, _ = split_fired_tool(stored.name, stored.kwargs, settings().tool_name_arg)
        return ScheduleListRow(
            name=stored.name,
            tool=fired_tool,
            cadence=stored.cadence,
            enabled=stored.enabled,
            next_run_at_ts=stored.next_run_at_ts,
            error=stored.error,
        )

    @tool
    async def backend_list_schedules() -> list[dict[str, Any]]:
        """List every schedule, enabled or disabled.

        Each row carries the schedule's ``name``, the ``tool`` it fires, its ``cadence``,
        ``enabled``, its next run (``next_run_at_ts`` and ``next_run_at_iso``, ``None`` when it
        has none) and its last scheduling ``error`` — never its arguments.
        """
        return [listing_row(stored).model_dump(mode="json") for stored in await store.read_all()]

    @tool
    async def backend_get_schedule(name: str) -> dict[str, Any]:
        """Get one schedule: its listing row plus the ``args`` and ``kwargs`` it fires the tool with.

        Replies ``{"status": "not_found", "name": name}`` when no schedule has that name.
        """
        stored = await store.read(name)
        if stored is None:
            return status("not_found", name)
        row = listing_row(stored)
        _, kwargs = split_fired_tool(stored.name, stored.kwargs, settings().tool_name_arg)
        detail = ScheduleDetail(**row.model_dump(exclude={"next_run_at_iso"}), args=stored.args, kwargs=kwargs)
        return detail.model_dump(mode="json")

    @tool
    async def backend_update_schedule(
        name: str,
        new_schedule: int | float | str | dict[str, Any] | None = None,
        next_run_in_ms: int | None = None,
        next_run_at_ts: float | None = None,
    ) -> dict[str, Any]:
        """Change a schedule's cadence, its next run, or both, in one step.

        - ``new_schedule``: seconds (int/float), a 5-field crontab string, or a cadence dict; the
          new cadence. Omitted, the cadence is kept.
        - ``next_run_in_ms``: the next run, in milliseconds from now.
        - ``next_run_at_ts``: the next run, as a POSIX timestamp. Ignored when ``next_run_in_ms``
          is given.

        A new cadence without a next run takes its next run from the backend's own rule for a new
        cadence. Replies ``{"status": "updated", "name", "previous_schedule", "new_schedule",
        "next_run_at_ts", "next_run_at_iso"}``; ``{"status": "skipped", "name", "message"}`` when
        nothing is given; ``{"status": "not_found", "name"}`` when no schedule has that name.
        """
        previous = await store.read(name)
        if previous is None:
            return status("not_found", name)
        if new_schedule is None and next_run_in_ms is None and next_run_at_ts is None:
            return {**status("skipped", name), "message": NO_CHANGES}
        cadence = None
        if new_schedule is not None:
            cadence = normalize_schedule(new_schedule)
            check_cadence_supported(store, cadence, label)
        if next_run_in_ms is not None:
            next_run_at_ts = datetime.now(UTC).timestamp() + next_run_in_ms / 1000.0
        if not await store.update(name, cadence=cadence, next_run_at_ts=next_run_at_ts):
            return status("not_found", name)
        current = await store.read(name)
        if current is None:
            return status("not_found", name)
        row = listing_row(current)
        return {
            **status("updated", name),
            "previous_schedule": previous.cadence.model_dump(mode="json"),
            "new_schedule": row.cadence.model_dump(mode="json"),
            "next_run_at_ts": row.next_run_at_ts,
            "next_run_at_iso": row.next_run_at_iso,
        }

    @tool
    async def backend_enable_schedule(name: str) -> dict[str, Any]:
        """Enable a schedule, so it fires on its cadence again.

        Replies ``{"status": "enabled", "name"}``, or ``not_found`` when no schedule has that
        name. On a backend that holds no disabled schedule, every stored schedule is enabled
        already, and nothing is written.
        """
        if not store.holds_disabled:
            return status("enabled" if await store.exists(name) else "not_found", name)
        return status("enabled" if await store.set_enabled(name, True) else "not_found", name)

    @tool
    async def backend_disable_schedule(name: str) -> dict[str, Any]:
        """Disable a schedule: it is kept, and fires no more until it is enabled.

        Replies ``{"status": "disabled", "name"}``, or ``not_found`` when no schedule has that
        name. A backend that holds no disabled schedule refuses the call with
        ``UnsupportedBackendTool`` and leaves the schedule as it is.
        """
        if not store.holds_disabled:
            raise UnsupportedBackendTool(label, "backend_disable_schedule")
        return status("disabled" if await store.set_enabled(name, False) else "not_found", name)

    @tool
    async def backend_run_schedule_now(name: str) -> dict[str, Any]:
        """Run a schedule at once; its cadence is kept.

        Replies ``{"status": "queued", "name"}``, or ``not_found`` when no schedule has that name.
        """
        return status("queued" if await store.run_now(name) else "not_found", name)

    @tool
    async def backend_schedule_exists(name: str) -> bool:
        """Whether a schedule with the given name exists."""
        return await store.exists(name)

    @tool
    async def backend_delete_schedule(name: str) -> dict[str, Any]:
        """Delete a schedule.

        Replies ``{"status": "deleted", "name"}``, or ``not_found`` when no schedule has that name.
        """
        return status("deleted" if await store.delete(name) else "not_found", name)

    @tool
    async def backend_export_schedules() -> list[dict[str, Any]]:
        """Export every schedule as a ``ScheduleRecord`` dump that ``backend_import_schedules`` takes back.

        Each record carries the schedule's ``name``, the ``tool`` it fires, its ``args`` and
        ``kwargs``, its ``cadence`` and ``enabled`` — its definition, never its runtime state.
        """
        records: list[dict[str, Any]] = []
        for stored in await store.read_all():
            fired_tool, kwargs = split_fired_tool(stored.name, stored.kwargs, settings().tool_name_arg)
            record = ScheduleRecord(
                name=stored.name,
                tool=fired_tool,
                args=stored.args,
                kwargs=kwargs,
                cadence=stored.cadence,
                enabled=stored.enabled,
            )
            records.append(record.model_dump(mode="json"))
        return records

    @tool
    async def backend_import_schedules(
        schedules: list[dict[str, Any]], mode: ScheduleImportMode = "skip"
    ) -> dict[str, Any]:
        """Import schedule records produced by ``backend_export_schedules``, keyed by name.

        Each entry is validated as a ``ScheduleRecord`` and written as the schedule of its name.
        Under ``mode="skip"`` (the default) a name already stored is left untouched and counted in
        ``skipped_existing``; under ``overwrite`` it is replaced. A new name is created under both.

        A record the backend cannot represent is refused as an error row before any write: a
        relative interval on a backend that honours none, and a disabled record on a backend that
        holds no disabled schedule (counted in ``skipped`` too). A record that fails validation or
        its write is an error row with its reason; the other records still import.

        Replies a ``ScheduleImportReport`` dump: ``created``, ``updated``, ``skipped``,
        ``skipped_existing`` and ``errors`` (each ``{"index", "name", "error"}``).
        """
        created = updated = skipped = skipped_existing = 0
        errors: list[ScheduleImportError] = []
        for index, entry in enumerate(schedules):
            raw_name = entry.get("name") if isinstance(entry, dict) else None
            entry_name = raw_name if isinstance(raw_name, str) else None
            try:
                record = ScheduleRecord.model_validate(entry)
                check_cadence_supported(store, record.cadence, label)
                if not record.enabled and not store.holds_disabled:
                    skipped += 1
                    errors.append(
                        ScheduleImportError(
                            index=index, name=record.name, error=disabled_import_refusal(record.name, label)
                        )
                    )
                    continue
                exists = await store.exists(record.name)
                if exists and mode == "skip":
                    skipped_existing += 1
                    continue
                await store.write(record)
                if exists:
                    updated += 1
                else:
                    created += 1
            except Exception as exc:
                errors.append(ScheduleImportError(index=index, name=entry_name, error=repr(exc)))
        return ScheduleImportReport(
            created=created, updated=updated, skipped=skipped, skipped_existing=skipped_existing, errors=errors
        ).model_dump()
