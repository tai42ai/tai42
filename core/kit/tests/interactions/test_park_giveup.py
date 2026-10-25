"""The give-up handler seam: each handler is handed the typed failure and the first owner's outcome is returned."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

import pytest
from tai42_contract.errors import ErrorKind

from tai42_kit.interactions import park_giveup
from tai42_kit.interactions.park_giveup import (
    ParkGiveUpFailure,
    ParkGiveUpOutcome,
    fire_park_giveup,
    register_park_giveup_handler,
)

_FAILURE = ParkGiveUpFailure({"abandoned": True}, "gave up", "ResumeAbandoned", ErrorKind.UNAVAILABLE, True)


@pytest.fixture(autouse=True)
def _restore_handlers() -> Iterator[None]:
    saved = list(park_giveup._park_giveup_handlers)
    park_giveup._park_giveup_handlers.clear()
    yield
    park_giveup._park_giveup_handlers[:] = saved


async def test_the_first_owning_handler_answers_and_later_ones_are_not_called() -> None:
    calls: list[tuple[str, str, ParkGiveUpFailure]] = []

    async def not_mine(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome | None:
        calls.append(("first", interaction_id, failure))
        return None

    async def mine(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome | None:
        calls.append(("second", interaction_id, failure))
        return ParkGiveUpOutcome({"answer": 42})

    async def after(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome | None:
        calls.append(("third", interaction_id, failure))
        return ParkGiveUpOutcome("never")

    register_park_giveup_handler(not_mine)
    register_park_giveup_handler(mine)
    register_park_giveup_handler(after)

    handled = await fire_park_giveup("i-1", _FAILURE)

    assert handled == ParkGiveUpOutcome({"answer": 42})
    assert calls == [("first", "i-1", _FAILURE), ("second", "i-1", _FAILURE)]
    assert calls[0][2] is _FAILURE
    assert calls[1][2] is _FAILURE


async def test_no_owner_returns_none() -> None:
    async def not_mine(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome | None:
        return None

    register_park_giveup_handler(not_mine)
    assert await fire_park_giveup("i-1", _FAILURE) is None


async def test_a_handlers_raise_propagates() -> None:
    async def broken(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome | None:
        raise RuntimeError("store down")

    register_park_giveup_handler(broken)
    with pytest.raises(RuntimeError, match="store down"):
        await fire_park_giveup("i-1", _FAILURE)


def test_the_failure_fields_are_ordered_and_all_required() -> None:
    assert ParkGiveUpFailure._fields == ("outcome", "message", "error_type", "kind", "transport")
    assert ParkGiveUpFailure._field_defaults == {}
    with pytest.raises(TypeError):
        ParkGiveUpFailure({"x": 1}, "m", "T", ErrorKind.UPSTREAM_ERROR)  # pyright: ignore[reportCallIssue]


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
