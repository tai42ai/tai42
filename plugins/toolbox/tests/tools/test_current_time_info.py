"""Tests for the ``current_time_info`` tool: structured time shape and its
registration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from tai42_toolbox.tools.current_time_info import current_time_info


def test_current_time_info_shape() -> None:
    info = asyncio.run(current_time_info())

    assert set(info) == {"utc", "local", "system"}

    utc = info["utc"]
    assert isinstance(utc["iso"], str)
    assert isinstance(utc["timestamp_ms"], int)
    for part in ("year", "month", "day", "hour", "minute", "second", "microsecond"):
        assert isinstance(utc[part], int)
    assert utc["iso"].endswith("+00:00")

    local = info["local"]
    assert isinstance(local["timezone_name"], str)
    assert isinstance(local["utc_offset"], str)

    system = info["system"]
    assert isinstance(system["epoch_seconds"], float)
    assert isinstance(system["epoch_nanoseconds"], int)


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_toolbox.tools.current_time_info")
    assert set(app.tools.registered) == {"current_time_info"}
