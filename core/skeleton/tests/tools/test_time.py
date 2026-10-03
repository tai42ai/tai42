"""The platform server clock: a typed structure read from the host, no tool dispatch."""

from __future__ import annotations

from datetime import UTC, datetime

from tai42_skeleton.tools.time import ServerTime, server_time


def test_server_time_returns_a_typed_structure() -> None:
    result = server_time()
    assert isinstance(result, ServerTime)
    # The UTC leg parses back to an aware datetime close to now.
    parsed = datetime.fromisoformat(result.utc.iso)
    assert parsed.tzinfo is not None
    delta = abs((datetime.now(UTC) - parsed).total_seconds())
    assert delta < 60


def test_server_time_dump_carries_the_three_legs() -> None:
    dumped = server_time().model_dump()
    assert set(dumped) == {"utc", "local", "system"}
    assert dumped["utc"]["iso"].endswith("+00:00")
    assert isinstance(dumped["system"]["epoch_nanoseconds"], int)
    assert dumped["local"]["timezone_name"]
