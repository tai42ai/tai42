"""The kill seam: drivers register at import, the platform fires every handler on each node of a killed run."""

from __future__ import annotations

import pytest

from tai42_kit.interactions import park_kill
from tai42_kit.interactions.park_kill import (
    ParkKillHandler,
    ParkKillNotReadyError,
    ParkKillOwnerMissingError,
    fire_park_killed,
    register_park_kill_handler,
)


@pytest.fixture(autouse=True)
def _fresh_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(park_kill, "_park_kill_handlers", [])


def _probe_driver(name: str, owned: set[str], ended: list[tuple[str, str, str]]) -> ParkKillHandler:
    """A neutral driver: ends the super-step of every node it owns and answers whether it owned the node."""

    async def _end(node: str, reason: str) -> bool:
        if node not in owned:
            return False
        ended.append((name, node, reason))
        return True

    return _end


async def test_every_registered_driver_is_fired_in_registration_order() -> None:
    ended: list[tuple[str, str, str]] = []
    register_park_kill_handler(_probe_driver("first", {"i-1"}, ended), continuation_tool="first_resume")
    register_park_kill_handler(_probe_driver("second", {"i-1", "i-2"}, ended), continuation_tool="second_resume")

    await fire_park_killed("i-1", "thread deleted", continuation_tool="first_resume")
    await fire_park_killed("i-2", "operator kill", continuation_tool="second_resume")

    assert ended == [
        ("first", "i-1", "thread deleted"),
        ("second", "i-1", "thread deleted"),
        ("second", "i-2", "operator kill"),
    ]


async def test_every_handler_is_fired_and_the_node_is_owned_when_any_owns_it() -> None:
    ended: list[tuple[str, str, str]] = []
    register_park_kill_handler(_probe_driver("not-ours", set(), ended), continuation_tool="other_resume")
    register_park_kill_handler(_probe_driver("owner", {"i-1"}, ended), continuation_tool="owner_resume")

    assert await fire_park_killed("i-1", "operator kill", continuation_tool="owner_resume") is None
    assert ended == [("owner", "i-1", "operator kill")]


async def test_a_chained_call_key_nobody_owns_raises_owner_missing() -> None:
    register_park_kill_handler(_probe_driver("driver", set(), []), continuation_tool="driver_resume")

    with pytest.raises(ParkKillOwnerMissingError, match="k-1") as caught:
        await fire_park_killed("k-1", "operator kill", continuation_tool=None)
    assert isinstance(caught.value, ParkKillNotReadyError)
    assert caught.value.node == "k-1"


async def test_a_park_whose_registered_driver_does_not_own_it_yet_raises_owner_missing() -> None:
    register_park_kill_handler(_probe_driver("driver", set(), []), continuation_tool="r")

    with pytest.raises(ParkKillOwnerMissingError):
        await fire_park_killed("i-1", "operator kill", continuation_tool="r")
    # A park whose continuation no registered driver binds belongs to a driver that keeps no state.
    await fire_park_killed("i-1", "operator kill", continuation_tool="other")


async def test_a_not_ready_raise_propagates_and_later_handlers_are_not_fired() -> None:
    ended: list[tuple[str, str, str]] = []

    async def _held(node: str, reason: str) -> bool:
        raise ParkKillNotReadyError(node, "a drive holds its lease")

    register_park_kill_handler(_held, continuation_tool="held_resume")
    register_park_kill_handler(_probe_driver("later", {"i-1"}, ended), continuation_tool="later_resume")

    with pytest.raises(ParkKillNotReadyError, match="a drive holds its lease"):
        await fire_park_killed("i-1", "operator kill", continuation_tool="held_resume")
    assert ended == []


async def test_with_no_driver_registered_a_park_is_owned_by_nobody_and_that_is_no_error() -> None:
    await fire_park_killed("i-1", "operator kill", continuation_tool="any_resume")
