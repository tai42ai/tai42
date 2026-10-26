"""Response models for the schedules operations."""

from __future__ import annotations

from pydantic import RootModel
from tai42_contract.backend import ScheduleListRow


class ScheduleListResponse(RootModel[list[ScheduleListRow]]):
    """The bare array of the scheduling backend's typed schedule rows."""
