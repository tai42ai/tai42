"""Response models for the tool-runs operations: background submission, a run's view and the run list."""

from __future__ import annotations

from pydantic import BaseModel, JsonValue, RootModel


class RunSubmitted(BaseModel):
    """A background tool-run submission acknowledgement (served with ``202``)."""

    run_id: str


class ToolRunView(BaseModel):
    """A background tool run's full status view.

    ``finished_at``/``result``/``error``/``resumed_interactions`` are present only once the run reaches a
    terminal status, so they are optional; ``result`` is the tool's arbitrary output;
    ``resumed_interactions`` is the parked interaction ids the run resumed or took while it executed
    (``[]`` when it resumed nothing).

    ``outcome`` is present only on a ``failed`` record whose run reached a failed terminal (a run that
    raises the contract's ``RunTerminalFailed``): the driver's outcome, whole, as the sync run-tool door
    delivers it under the same name; ``error`` is then the plain ``tool run failed``. A plain raise records
    its message under ``error`` and no ``outcome``.
    """

    run_id: str
    tool_name: str
    status: str
    started_at: str
    finished_at: str | None = None
    result: JsonValue | None = None
    error: str | None = None
    outcome: JsonValue | None = None
    resumed_interactions: list[str] | None = None


class ToolRunListItem(BaseModel):
    """One background tool run in the list view — never ``result``/``error``.

    ``finished_at`` is present only for a terminal run.
    """

    run_id: str
    tool_name: str
    status: str
    started_at: str
    finished_at: str | None = None


class ToolRunListResponse(RootModel[list[ToolRunListItem]]):
    """The bare array of a tool's recent runs, newest first."""
