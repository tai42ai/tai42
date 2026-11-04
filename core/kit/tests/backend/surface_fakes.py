"""The tool registry, the schedule store and the settings the ``backend_*`` surface tests run over."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRecord, StoredSchedule

from tai42_kit.backend.settings import BackendDispatchSettings

#: The tool-name argument the fake backend's stored job kwargs carry the fired tool under.
TOOL_ARG = "backend_tool_name"

#: The backend package the fake surface is registered as.
OWNER = "tai42_backend_fake"


@dataclass
class RegisteredTool:
    func: Callable[..., Any]
    tags: set[str]
    module: str


class SurfaceTools:
    """Records each tool as registered: its function, its tags, the module it names at that moment."""

    def __init__(self) -> None:
        self.registered: dict[str, RegisteredTool] = {}

    def tool(self, *, tags: set[str]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(func: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[func.__name__] = RegisteredTool(func, set(tags), func.__module__)
            return func

        return register

    def __getitem__(self, name: str) -> Callable[..., Any]:
        return self.registered[name].func


class SurfaceApp:
    """The ``tai42_app`` target a surface test binds: a tool registry and nothing else."""

    def __init__(self) -> None:
        self.tools = SurfaceTools()


def surface_settings() -> BackendDispatchSettings:
    """The fake backend's settings: its stored job kwargs carry the fired tool under :data:`TOOL_ARG`."""
    return BackendDispatchSettings(tool_name_arg=TOOL_ARG)


@dataclass
class FakeStore:
    """An in-memory schedule store with the capabilities a test declares."""

    holds_disabled: bool = True
    relative_intervals: bool = False
    schedules: dict[str, StoredSchedule] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)
    writes: list[ScheduleRecord] = field(default_factory=list)
    fail_write: str | None = None
    vanish_on_update: bool = False
    vanish_after_update: bool = False

    def seed(
        self,
        name: str,
        *,
        tool: str = "sample_tool",
        args: list[Any] | None = None,
        cadence: IntervalCadence | CrontabCadence | None = None,
        enabled: bool = True,
        next_run_at_ts: float | None = 100.0,
        **kwargs: Any,
    ) -> StoredSchedule:
        stored = StoredSchedule(
            name=name,
            args=[1] if args is None else args,
            kwargs={TOOL_ARG: tool, **kwargs},
            cadence=cadence or IntervalCadence(every=60),
            enabled=enabled,
            next_run_at_ts=next_run_at_ts,
        )
        self.schedules[name] = stored
        return stored

    async def write(self, record: ScheduleRecord) -> None:
        if record.name == self.fail_write:
            raise RuntimeError(f"write of {record.name} failed")
        self.writes.append(record)
        self.seed(
            record.name,
            tool=record.tool,
            args=record.args,
            cadence=record.cadence,
            enabled=record.enabled,
            next_run_at_ts=None,
            **record.kwargs,
        )

    async def exists(self, name: str) -> bool:
        return name in self.schedules

    async def read(self, name: str) -> StoredSchedule | None:
        return self.schedules.get(name)

    async def read_all(self) -> list[StoredSchedule]:
        return list(self.schedules.values())

    async def delete(self, name: str) -> bool:
        return self.schedules.pop(name, None) is not None

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        stored = self.schedules.get(name)
        if stored is None:
            return False
        self.schedules[name] = stored.model_copy(update={"enabled": enabled})
        return True

    async def run_now(self, name: str) -> bool:
        if name not in self.schedules:
            return False
        self.ran.append(name)
        return True

    async def update(
        self,
        name: str,
        *,
        cadence: IntervalCadence | CrontabCadence | None,
        next_run_at_ts: float | None,
    ) -> bool:
        if self.vanish_on_update:
            self.schedules.pop(name, None)
            return False
        stored = self.schedules[name]
        changes: dict[str, Any] = {}
        if cadence is not None:
            changes["cadence"] = cadence
        changes["next_run_at_ts"] = next_run_at_ts if next_run_at_ts is not None else 500.0
        self.schedules[name] = stored.model_copy(update=changes)
        if self.vanish_after_update:
            del self.schedules[name]
        return True
