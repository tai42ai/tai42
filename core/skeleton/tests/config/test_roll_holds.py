"""The named holds a handed-off roll's carrier enters for the roll's whole run.

A component registers a hold under a name; the carrier enters the holds a roll names in order and
exits them in reverse once the roll ended. A name nothing registered raises before any hold is
entered, and a second registration under one name raises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest

from tai42_skeleton.config import roll_holds
from tai42_skeleton.config.roll_holds import enter_roll_holds, register_roll_hold


@pytest.fixture(autouse=True)
def _no_registered_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(roll_holds, "_HOLDS", {})


def _recording(name: str, events: list[str]) -> Callable[[], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def hold() -> AsyncIterator[None]:
        events.append(f"enter {name}")
        try:
            yield
        finally:
            events.append(f"exit {name}")

    return hold


async def test_the_named_holds_are_entered_in_order_and_exited_in_reverse() -> None:
    events: list[str] = []
    register_roll_hold("first", _recording("first", events))
    register_roll_hold("second", _recording("second", events))

    async with enter_roll_holds(["first", "second"]):
        events.append("roll")

    assert events == ["enter first", "enter second", "roll", "exit second", "exit first"]


async def test_no_names_enter_nothing() -> None:
    async with enter_roll_holds([]):
        pass


async def test_an_unregistered_name_raises_before_any_hold_is_entered() -> None:
    events: list[str] = []
    register_roll_hold("first", _recording("first", events))

    with pytest.raises(LookupError, match="'missing'"):
        async with enter_roll_holds(["first", "missing"]):
            pytest.fail("the body runs with a hold missing")

    assert events == []


async def test_a_hold_whose_entry_fails_exits_the_holds_already_entered() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def refused() -> AsyncIterator[None]:
        raise ConnectionError("the store refused the session")
        yield

    register_roll_hold("first", _recording("first", events))
    register_roll_hold("refused", refused)

    with pytest.raises(ConnectionError, match="refused the session"):
        async with enter_roll_holds(["first", "refused"]):
            pytest.fail("the body runs with a hold not entered")

    assert events == ["enter first", "exit first"]


def test_a_second_registration_under_one_name_raises() -> None:
    register_roll_hold("first", _recording("first", []))

    with pytest.raises(ValueError, match="'first' is already registered"):
        register_roll_hold("first", _recording("first", []))
