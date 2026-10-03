"""The server clock — a typed platform helper answering the host's own time.

The platform reads its own wall clock from here; it never dispatches a tool by name
for its own time. The structure carries UTC details, the local-system-time details
(timezone name, offset, broken-down parts), and high-precision system timestamps, so a
caller reads whichever precision it needs.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel


class UtcTime(BaseModel):
    """The current instant in UTC, broken down."""

    iso: str
    timestamp_ms: int
    date: str
    time: str
    weekday: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    microsecond: int


class LocalTime(BaseModel):
    """The current instant in the host's local timezone, broken down."""

    iso: str
    timestamp_ms: int
    timezone_name: str
    utc_offset: str
    date: str
    time: str
    weekday: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    microsecond: int


class SystemTime(BaseModel):
    """High-precision system timestamps."""

    epoch_seconds: float
    epoch_nanoseconds: int


class ServerTime(BaseModel):
    """The server's current date and time in UTC, local, and raw-system forms."""

    utc: UtcTime
    local: LocalTime
    system: SystemTime


def server_time() -> ServerTime:
    """The host's current date and time as a typed structure."""
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone()
    return ServerTime(
        utc=UtcTime(
            iso=now_utc.isoformat(),
            timestamp_ms=int(now_utc.timestamp() * 1000),
            date=now_utc.strftime("%Y-%m-%d"),
            time=now_utc.strftime("%H:%M:%S"),
            weekday=now_utc.strftime("%A"),
            year=now_utc.year,
            month=now_utc.month,
            day=now_utc.day,
            hour=now_utc.hour,
            minute=now_utc.minute,
            second=now_utc.second,
            microsecond=now_utc.microsecond,
        ),
        local=LocalTime(
            iso=now_local.isoformat(),
            timestamp_ms=int(now_local.timestamp() * 1000),
            timezone_name=str(now_local.tzinfo),
            utc_offset=now_local.strftime("%z"),
            date=now_local.strftime("%Y-%m-%d"),
            time=now_local.strftime("%H:%M:%S"),
            weekday=now_local.strftime("%A"),
            year=now_local.year,
            month=now_local.month,
            day=now_local.day,
            hour=now_local.hour,
            minute=now_local.minute,
            second=now_local.second,
            microsecond=now_local.microsecond,
        ),
        system=SystemTime(epoch_seconds=time.time(), epoch_nanoseconds=time.time_ns()),
    )


__all__ = ["LocalTime", "ServerTime", "SystemTime", "UtcTime", "server_time"]
