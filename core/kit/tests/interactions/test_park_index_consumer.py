"""A neutral consumer of the park index: a test-only driver that parks, resumes, re-parks, is killed and replays.

The driver below is neither of the platform's real drivers: it keeps its own entry payload
(``{"step": <label>}``) under its own namespace, and drives its super-steps with nothing but the
library's public API — proof that any driver resuming through the platform's continuation seam can
use the index as it is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from tai42_contract.interactions import RunFailed, SuspendedInteraction

from tai42_kit.interactions.park_index import (
    DriveInProgressError,
    ParkIndex,
    ResolutionRecord,
    entry_ttl_seconds,
    superstep_id,
)

RUN = "probe-run-1"


class _ProbeDriver:
    """A minimal driver: parks a step of asks, buffers answers, drives the step once all are in."""

    def __init__(self, index: ParkIndex) -> None:
        self.index = index
        self.drives: list[dict[str, Any]] = []
        # The asks of each step label, the driver's own record of what it parked.
        self.known: dict[str, list[str]] = {}

    async def park(self, asks: Sequence[str], label: str) -> str:
        step = superstep_id(asks)
        await self.index.persist(
            thread_id=RUN,
            superstep=step,
            entries={ask: {"step": label} for ask in asks},
            expected=dict.fromkeys(asks, label),
            entry_ttl={ask: entry_ttl_seconds(None) for ask in asks},
            barrier_ttl=entry_ttl_seconds(None),
        )
        return step

    async def answer(self, ask: str, value: Any) -> Any:
        """The driver's continuation: replay a resolved step, buffer, and drive the completed step."""
        entry = await self.index.read_entry(ask)
        assert entry is not None
        if self.index.tombstone_kind(entry) != "live":
            record = await self.index.read_tombstone_resolution(entry)
            return None if record is None else record.value
        step = superstep_id(self._members(entry))
        progress = await self.index.buffer(RUN, step, ask, value)
        if progress.remaining:
            return {"waiting_on": progress.remaining}
        async with self.index.claim(RUN, step) as lease:
            barrier = await self.index.read_barrier(RUN, step)
            assert barrier is not None
            self.drives.append(dict(barrier.outputs))
            outcome = {"answers": dict(barrier.outputs)}
            await self.index.finalize(lease, member_ids=list(barrier.expected), resolution="terminal", value=outcome)
        return outcome

    async def kill(self, ask: str) -> None:
        """The driver's kill: claim, drop every other resolved step, finalize aborted."""
        entry = await self.index.read_entry(ask)
        assert entry is not None
        step = superstep_id(self._members(entry))
        async with self.index.claim(RUN, step) as lease:
            barrier = await self.index.read_barrier(RUN, step)
            assert barrier is not None
            await self.index.drop_run_resolutions(RUN, keep=step)
            await self.index.finalize(
                lease,
                member_ids=list(barrier.expected),
                resolution="aborted",
                value=RunFailed(outcome={"status": "killed"}),
            )

    def _members(self, entry: dict[str, Any]) -> list[str]:
        return self.known[entry["step"]]


@pytest.fixture
def driver(make_index: Callable[[str], ParkIndex]) -> _ProbeDriver:
    return _ProbeDriver(make_index("probe:park"))


async def test_two_asks_in_one_step_drive_once_when_both_are_answered(driver: _ProbeDriver) -> None:
    driver.known["s1"] = ["ask-a", "ask-b"]
    await driver.park(["ask-a", "ask-b"], "s1")

    assert await driver.answer("ask-a", "first") == {"waiting_on": ["ask-b"]}
    outcome = await driver.answer("ask-b", "second")

    assert outcome == {"answers": {"ask-a": "first", "ask-b": "second"}}
    assert driver.drives == [{"ask-a": "first", "ask-b": "second"}]
    # A lapped redelivery replays the stored outcome without driving again.
    assert await driver.answer("ask-a", "late") == outcome
    assert len(driver.drives) == 1


async def test_a_re_park_resolves_the_old_step_suspended_and_parks_a_new_one(driver: _ProbeDriver) -> None:
    index = driver.index
    driver.known["s1"] = ["ask-a"]
    driver.known["s2"] = ["ask-c"]
    first = await driver.park(["ask-a"], "s1")
    await index.buffer(RUN, first, "ask-a", "go")

    async with index.claim(RUN, first) as lease:
        re_park = SuspendedInteraction(interaction_id="ask-c")
        await index.finalize(lease, member_ids=["ask-a"], resolution="suspended", value=re_park)
    second = await driver.park(["ask-c"], "s2")

    assert await index.read_resolution(RUN, first) == ResolutionRecord("suspended", re_park)
    assert await index.threads_with_live_barriers([RUN]) == {RUN}
    assert await index.read_barrier(RUN, second) is not None


async def test_a_kill_is_refused_while_a_drive_holds_the_lease_and_lands_after(driver: _ProbeDriver) -> None:
    index = driver.index
    driver.known["s1"] = ["ask-a"]
    step = await driver.park(["ask-a"], "s1")

    async with index.claim(RUN, step):
        with pytest.raises(DriveInProgressError):
            await driver.kill("ask-a")
    await driver.kill("ask-a")

    record = await index.read_resolution(RUN, step)
    assert record == ResolutionRecord("aborted", RunFailed(outcome={"status": "killed"}))
    assert await index.read_barrier(RUN, step) is None
    assert await index.threads_with_live_barriers([RUN]) == set()


async def test_a_resolved_step_replays_its_stored_run_failed(driver: _ProbeDriver) -> None:
    driver.known["s1"] = ["ask-a"]
    await driver.park(["ask-a"], "s1")
    await driver.kill("ask-a")

    replayed = await driver.answer("ask-a", "too late")

    assert replayed == RunFailed(outcome={"status": "killed"})
    assert isinstance(replayed, RunFailed)
    assert driver.drives == []
