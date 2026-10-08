"""The give-up handler seam: handlers run in registration order and the first owner's outcome is returned."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from tai42_kit.interactions import park_giveup
from tai42_kit.interactions.park_giveup import (
    ParkGiveUpOutcome,
    fire_park_giveup,
    register_park_giveup_handler,
)


@pytest.fixture(autouse=True)
def _restore_handlers() -> Iterator[None]:
    saved = list(park_giveup._park_giveup_handlers)
    park_giveup._park_giveup_handlers.clear()
    yield
    park_giveup._park_giveup_handlers[:] = saved


async def test_the_first_owning_handler_answers_and_later_ones_are_not_called() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def not_mine(interaction_id: str, outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        calls.append(("first", interaction_id, dict(outcome)))
        return None

    async def mine(interaction_id: str, outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        calls.append(("second", interaction_id, dict(outcome)))
        return ParkGiveUpOutcome({"answer": 42})

    async def after(interaction_id: str, outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        calls.append(("third", interaction_id, dict(outcome)))
        return ParkGiveUpOutcome("never")

    register_park_giveup_handler(not_mine)
    register_park_giveup_handler(mine)
    register_park_giveup_handler(after)

    handled = await fire_park_giveup("i-1", {"abandoned": True})

    assert handled == ParkGiveUpOutcome({"answer": 42})
    assert calls == [("first", "i-1", {"abandoned": True}), ("second", "i-1", {"abandoned": True})]


async def test_no_owner_returns_none() -> None:
    async def not_mine(interaction_id: str, outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        return None

    register_park_giveup_handler(not_mine)
    assert await fire_park_giveup("i-1", {}) is None


async def test_a_handlers_raise_propagates() -> None:
    async def broken(interaction_id: str, outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        raise RuntimeError("store down")

    register_park_giveup_handler(broken)
    with pytest.raises(RuntimeError, match="store down"):
        await fire_park_giveup("i-1", {})


def test_the_module_adds_only_standard_library_modules() -> None:
    probe = (
        "import sys\n"
        "import tai42_kit.interactions\n"
        "before = set(sys.modules)\n"
        "import tai42_kit.interactions.park_giveup\n"
        "added = sorted(m.split('.')[0] for m in set(sys.modules) - before)\n"
        "print('\\n'.join(sorted(set(added))))\n"
    )
    added = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True).stdout.split()
    assert set(added) <= set(sys.stdlib_module_names) | {"tai42_kit"}, added
