"""The ``current_time_info`` tool: current time as a structured object.

No heavy backing dependency, so this module imports cleanly in the base install.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(tags={"time"})
async def current_time_info() -> dict[str, Any]:
    """Return the current time as a structured object.

    Contains UTC details, local-system-time details (timezone name, offset,
    broken-down parts), and high-precision system timestamps.
    """
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone()

    return {
        "utc": {
            "iso": now_utc.isoformat(),
            "timestamp_ms": int(now_utc.timestamp() * 1000),
            "date": now_utc.strftime("%Y-%m-%d"),
            "time": now_utc.strftime("%H:%M:%S"),
            "weekday": now_utc.strftime("%A"),
            "year": now_utc.year,
            "month": now_utc.month,
            "day": now_utc.day,
            "hour": now_utc.hour,
            "minute": now_utc.minute,
            "second": now_utc.second,
            "microsecond": now_utc.microsecond,
        },
        "local": {
            "iso": now_local.isoformat(),
            "timestamp_ms": int(now_local.timestamp() * 1000),
            "timezone_name": str(now_local.tzinfo),
            "utc_offset": now_local.strftime("%z"),
            "date": now_local.strftime("%Y-%m-%d"),
            "time": now_local.strftime("%H:%M:%S"),
            "weekday": now_local.strftime("%A"),
            "year": now_local.year,
            "month": now_local.month,
            "day": now_local.day,
            "hour": now_local.hour,
            "minute": now_local.minute,
            "second": now_local.second,
            "microsecond": now_local.microsecond,
        },
        "system": {
            "epoch_seconds": time.time(),
            "epoch_nanoseconds": time.time_ns(),
        },
    }
