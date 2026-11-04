"""Engine-free seams the branch tools are registered over, and the app capturing their factories.

:class:`FakeTaskDoor` records what each branch submits and answers a wait with a set result or
error; :class:`FakeScheduleWriter` records each schedule written; :class:`BranchApp` is the app
``tai42_app`` binds to, capturing each registered extension factory by name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from pydantic_settings import SettingsConfigDict
from tai42_contract.backend import CallbackSchema, ScheduleRecord
from tai42_contract.extensions import ExtensionKind

from tai42_kit.backend import BackendDispatchSettings


class BranchSettings(BackendDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="BRANCHFAKE_")


@dataclass
class Submitted:
    """One submit the door received."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    options: dict[str, Any]
    callback: CallbackSchema | None


@dataclass
class FakeTaskDoor:
    """A task door whose handle is the submit's index; its wait returns ``result`` or raises ``error``."""

    task_opts: Mapping[str, Any] = field(default_factory=lambda: {"priority": int | None})
    result: Any = None
    error: BaseException | None = None
    submitted: list[Submitted] = field(default_factory=list)
    waits: list[tuple[int, float, bool]] = field(default_factory=list)
    lifecycle: FleetLifecycle | None = None

    async def submit(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], options: dict[str, Any], callback: CallbackSchema | None
    ) -> int:
        self.submitted.append(Submitted(args, kwargs, options, callback))
        return len(self.submitted) - 1

    def task_id(self, handle: int) -> str:
        return f"task-{handle}"

    async def wait(self, handle: int, timeout: float) -> Any:
        self.waits.append((handle, timeout, self.lifecycle is not None and self.lifecycle.waiting_on_fleet))
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeScheduleWriter:
    """A schedule writer recording each record it is handed."""

    relative_intervals: bool = False
    written: list[ScheduleRecord] = field(default_factory=list)

    async def write(self, record: ScheduleRecord) -> None:
        self.written.append(record)


class FleetLifecycle:
    """The lifecycle member a sync branch reaches for: the fleet-work wait span."""

    def __init__(self) -> None:
        self.waiting_on_fleet = False
        self.spans = 0

    @asynccontextmanager
    async def waiting_on_fleet_work(self) -> AsyncIterator[None]:
        self.spans += 1
        self.waiting_on_fleet = True
        try:
            yield
        finally:
            self.waiting_on_fleet = False


class CapturingExtensions:
    """The extensions facet, capturing each registered factory by name with its kind."""

    def __init__(self) -> None:
        self.factories: dict[str, tuple[ExtensionKind, Callable[..., Any]]] = {}

    def extension(self, f: Callable[..., Any], *, kind: ExtensionKind, name: str) -> Callable[..., Any]:
        self.factories[name] = (kind, f)
        return f


class BranchApp:
    """The app ``tai42_app`` binds to while branches are registered and dispatched."""

    def __init__(self) -> None:
        self.extensions = CapturingExtensions()
        self.lifecycle = FleetLifecycle()
