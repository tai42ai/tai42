"""The scheduling protocol every scheduling backend implements: names and data.

A scheduling backend registers the marker tools (:data:`SCHEDULE_LIST_TOOL`,
:data:`SCHEDULE_DELETE_TOOL`) plus the backup pair (:data:`SCHEDULE_EXPORT_TOOL`,
:data:`SCHEDULE_IMPORT_TOOL`), and a ``schedule_task`` tool extension
(:data:`SCHEDULE_EXTENSION`) that branches a tool into ``<tool>_schedule_task``
(:data:`SCHEDULE_BRANCH_SUFFIX`). The create door dispatches that branch with the
schedule's name under :data:`SCHEDULE_NAME_KEY` and its typed cadence, dumped, under
:data:`SCHEDULE_CADENCE_KEY`.

A cadence is a :data:`ScheduleCadence`: an :class:`IntervalCadence` or a
:class:`CrontabCadence`, discriminated by ``kind``. The marker tools speak
:class:`ScheduleListRow` (list), :class:`ScheduleRecord` (export/import) and
:class:`ScheduleImportReport` (import). The models are data only; parsing friendly
cadence specs is the kit's.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tai42_contract.interactions.door_contract import ParkableDoorMixin
from tai42_contract.states import StateBinding, StateSubject

SCHEDULE_LIST_TOOL: Final = "backend_list_schedules"
SCHEDULE_DELETE_TOOL: Final = "backend_delete_schedule"
SCHEDULE_EXPORT_TOOL: Final = "backend_export_schedules"
SCHEDULE_IMPORT_TOOL: Final = "backend_import_schedules"
#: The tools whose presence marks an installed scheduling backend.
SCHEDULE_MARKER_TOOLS: Final = (SCHEDULE_LIST_TOOL, SCHEDULE_DELETE_TOOL)
SCHEDULE_EXTENSION: Final = "schedule_task"
SCHEDULE_BRANCH_SUFFIX: Final = "_schedule_task"
#: The branch kwarg carrying the dumped :data:`ScheduleCadence`.
SCHEDULE_CADENCE_KEY: Final = "backend_schedule"
#: The branch kwarg carrying the schedule's name (its revocation handle).
SCHEDULE_NAME_KEY: Final = "backend_schedule_name"

_CRONTAB_FIELDS: Final = ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")


class IntervalCadence(BaseModel):
    """Fire every ``every`` seconds; ``relative`` aligns the schedule to the interval's own clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["interval"] = "interval"
    every: float = Field(gt=0, description="The interval in seconds.")
    relative: bool = False


class CrontabCadence(BaseModel):
    """Fire on a five-field crontab (minute, hour, day of month, month, day of week)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["crontab"] = "crontab"
    minute: str = "*"
    hour: str = "*"
    day_of_month: str = "*"
    month_of_year: str = "*"
    day_of_week: str = "*"

    @model_validator(mode="before")
    @classmethod
    def _expression(cls, data: Any) -> Any:
        """Split an ``expression`` (5 fields, or 6 with a leading seconds field) into the fields.

        Applies only to a mapping carrying ``expression`` and none of the field keys; a mapping
        carrying both keeps ``expression`` as an unknown key, which the model refuses.
        """
        if not isinstance(data, dict) or "expression" not in data:
            return data
        if any(field in data for field in _CRONTAB_FIELDS):
            return data
        rest: dict[str, Any] = {key: value for key, value in data.items() if key != "expression"}
        expression = data["expression"]
        if not isinstance(expression, str):
            raise ValueError("a crontab expression is a string")  # noqa: TRY004 pydantic reports ValueError
        parts = expression.split()
        if len(parts) == 6:
            parts = parts[1:]
        if len(parts) != 5:
            raise ValueError(f"a crontab expression has 5 fields (6 with seconds); got {len(parts)}")
        return {**rest, **dict(zip(_CRONTAB_FIELDS, parts, strict=True))}


ScheduleCadence = Annotated[IntervalCadence | CrontabCadence, Field(discriminator="kind")]


class ScheduleRecord(BaseModel):
    """One exported or imported schedule: the durable definition, never runtime state."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    tool: str = Field(min_length=1, description="The name of the tool the schedule fires.")
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="The tool arguments plus the reserved door stamps the fire needs.",
    )
    cadence: ScheduleCadence
    enabled: bool


class ScheduleRowError(BaseModel):
    """A backend's last scheduling failure for a schedule, when the backend records one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str
    at_iso: str | None = None


class ScheduleListRow(BaseModel):
    """One row of the schedule listing: no arguments, no kwargs."""

    model_config = ConfigDict(extra="forbid")

    name: str
    tool: str
    cadence: ScheduleCadence
    enabled: bool
    next_run_at_ts: float | None = None
    next_run_at_iso: str | None = None
    error: ScheduleRowError | None = None


ScheduleImportMode = Literal["skip", "overwrite"]


class ScheduleImportError(BaseModel):
    """One import row the backend refused, at its ``index`` in the rows it was handed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    name: str | None
    error: str


class ScheduleImportReport(BaseModel):
    """What a schedule import did: counts per outcome and the refused rows."""

    model_config = ConfigDict(extra="forbid")

    created: int
    updated: int
    skipped: int
    skipped_existing: int
    errors: list[ScheduleImportError]


class ScheduleCreate(ParkableDoorMixin):
    """Create a schedule that runs ``tool_name`` on ``cadence``, or once now when no cadence is given.

    ``tool_kwargs`` are the arguments each fire passes to the tool, the tool's alone and forwarded
    untouched — a tool whose OWN argument is named ``subject`` carries it here like any argument.
    ``subject`` is the OPTIONAL top-level door field naming the ``schedule``-door state context each
    fire re-establishes: the create door stamps it onto the job, and the fire keys its state writes and
    parks on it (it never reaches the base tool). ``state_binding`` is the OPTIONAL door-layer binding
    the fire applies around the started tool alone (never the base tool's). ``schedule_name`` names a
    recurring schedule; without it the door derives one from the tool, its arguments and the cadence.

    A schedule is a parkable-driving door: it carries the four :class:`ParkableDoorMixin` jqs
    (``start_expr`` builds the fired tool's kwargs; ``cancel_expr`` / ``resume_expr`` act on the run's
    parked interactions; ``extras_expr`` builds the started run's extras), each evaluated over the
    fired tool's arguments with the run's parked interactions bound as ``$parked``. Any contract jq
    needs an ``execution_key`` (the api-key ``user_id`` the recurring fire runs as, so a park it
    raises can be rebound and resumed under that authority).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, description="The base tool to run.")
    tool_kwargs: dict[str, Any] = Field(default_factory=dict)
    cadence: ScheduleCadence | None = Field(default=None, description="The cadence; none runs the tool once now.")
    schedule_name: str | None = Field(default=None, min_length=1)
    execution_key: str | None = Field(
        default=None,
        min_length=1,
        description="The api-key user_id a contract-bearing recurring fire runs as (required with any contract jq).",
    )
    subject: StateSubject | None = None
    state_binding: StateBinding | None = None

    @model_validator(mode="after")
    def _door_rules(self) -> ScheduleCreate:
        if self.execution_key is None and any(
            getattr(self, field) is not None for field in ("start_expr", "cancel_expr", "resume_expr", "extras_expr")
        ):
            raise ValueError("a schedule contract jq (start/cancel/resume/extras) requires an execution_key")
        if self.schedule_name is not None and self.cadence is None:
            raise ValueError("schedule_name needs a cadence; a run-once schedule has no name")
        if self.tool_name.endswith(SCHEDULE_BRANCH_SUFFIX):
            raise ValueError("tool_name names a schedule vehicle; pass the base tool and a cadence")
        return self
