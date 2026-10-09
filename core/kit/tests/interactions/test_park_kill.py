"""The whole-chain kill teardown seam: drivers register at import, the platform fires every handler in order."""

from __future__ import annotations

import pytest

from tai42_kit.interactions import park_kill
from tai42_kit.interactions.park_kill import ParkKillHandler, fire_park_killed, register_park_kill_handler


@pytest.fixture(autouse=True)
def _fresh_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(park_kill, "_park_kill_handlers", [])


def _probe_driver(name: str, owned: set[str], torn_down: list[tuple[str, str, str]]) -> ParkKillHandler:
    """A neutral driver teardown: tears down only the runs it owns, a no-op for any other."""

    async def _teardown(interaction_id: str, reason: str) -> None:
        if interaction_id in owned:
            torn_down.append((name, interaction_id, reason))

    return _teardown


async def test_every_registered_driver_is_fired_in_registration_order() -> None:
    torn_down: list[tuple[str, str, str]] = []
    register_park_kill_handler(_probe_driver("first", {"i-1"}, torn_down))
    register_park_kill_handler(_probe_driver("second", {"i-1", "i-2"}, torn_down))

    await fire_park_killed("i-1", "thread deleted")
    await fire_park_killed("i-2", "operator kill")

    assert torn_down == [
        ("first", "i-1", "thread deleted"),
        ("second", "i-1", "thread deleted"),
        ("second", "i-2", "operator kill"),
    ]


async def test_a_raising_handler_propagates_and_stops_the_pass() -> None:
    torn_down: list[tuple[str, str, str]] = []

    async def _not_ready(interaction_id: str, reason: str) -> None:
        raise RuntimeError(f"park record for {interaction_id} not written yet")

    register_park_kill_handler(_not_ready)
    register_park_kill_handler(_probe_driver("later", {"i-1"}, torn_down))

    with pytest.raises(RuntimeError, match="not written yet"):
        await fire_park_killed("i-1", "operator kill")
    assert torn_down == []


async def test_with_no_driver_registered_the_fire_is_a_no_op() -> None:
    await fire_park_killed("i-1", "operator kill")
