"""The schedule store: the vendor's durable home of a backend's schedules.

A backend implements :class:`ScheduleStore` over its own schedule storage; the platform's
schedule tools (list, get, update, enable, disable, run now, exists, delete, export, import)
read and write through it. Its :class:`ScheduleWriter` part is what every write door needs
(the schedule branch tool included): the one writer and the cadences it honours. A read hands
back a :class:`StoredSchedule`: the definition as stored, the fired tool's name still inside its
``kwargs``.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.backend.scheduling import (
    CrontabCadence,
    IntervalCadence,
    ScheduleCadence,
    ScheduleRecord,
    ScheduleRowError,
)


class StoredSchedule(BaseModel):
    """One schedule as its store holds it, with its runtime state.

    ``kwargs`` are the stored job kwargs: the fired tool's name under the backend's tool-name
    argument, the tool's arguments and the reserved stamps. ``next_run_at_ts`` is the next run's
    POSIX timestamp, ``None`` when the store records no next run. ``error`` is the store's last
    scheduling failure for the schedule, when it records one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    cadence: ScheduleCadence
    enabled: bool
    next_run_at_ts: float | None = None
    error: ScheduleRowError | None = None


class ScheduleWriter(Protocol):
    """A backend's schedule writer: the one write every schedule write door goes through.

    A write door checks a cadence against :attr:`relative_intervals` before it writes.
    """

    @property
    def relative_intervals(self) -> bool:
        """Whether the store honours an interval cadence with ``relative=True``."""
        ...

    async def write(self, record: ScheduleRecord) -> None:
        """Create or replace the schedule ``record`` names, as ``record`` defines it.

        The one writer behind the schedule branch, create, update and import. ``record.tool`` is
        stored inside the job kwargs under the backend's tool-name argument. The write is durable
        when this returns; it raises on a store failure.
        """
        ...


class ScheduleStore(ScheduleWriter, Protocol):
    """A backend's schedule storage: its capabilities, its reads and its writes.

    A write or a read raises on a store failure; ``False`` or ``None`` answers only "no such
    schedule".
    """

    @property
    def holds_disabled(self) -> bool:
        """Whether the store holds a disabled schedule (``enabled=False``) as such.

        A store that holds none keeps every schedule it stores enabled: :meth:`write` refuses a
        disabled record and :meth:`set_enabled` is never called on it.
        """
        ...

    async def exists(self, name: str) -> bool:
        """Whether a schedule named ``name`` is stored."""
        ...

    async def read(self, name: str) -> StoredSchedule | None:
        """The schedule named ``name``, or ``None`` when none is stored."""
        ...

    async def read_all(self) -> list[StoredSchedule]:
        """Every stored schedule, enabled or disabled; one deleted while the store is read is left out."""
        ...

    async def delete(self, name: str) -> bool:
        """Remove the schedule ``name``; ``False`` when none was stored."""
        ...

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable the schedule ``name``; ``False`` when none is stored."""
        ...

    async def run_now(self, name: str) -> bool:
        """Make the schedule ``name`` run at once, its recurrence kept; ``False`` when none is stored."""
        ...

    async def update(
        self,
        name: str,
        *,
        cadence: IntervalCadence | CrontabCadence | None,
        next_run_at_ts: float | None,
    ) -> bool:
        """Change the schedule ``name`` in one step; ``False`` when none is stored.

        ``cadence``, when given, replaces its cadence; ``next_run_at_ts``, when given, moves its
        next run to that POSIX timestamp. At least one is given. A new cadence without a
        ``next_run_at_ts`` takes its next run from the store's own rule for a new cadence.
        """
        ...
