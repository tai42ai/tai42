"""Schedules: the save rules every schedule definition runs, at the create door and on restore.

The schedule's storage stays the scheduling backend's own; this package owns only the checks a
schedule's definition passes before the backend stores it.
"""

from __future__ import annotations

from tai42_skeleton.schedules.definition import check_schedule_definition

__all__ = ["check_schedule_definition"]
