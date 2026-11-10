"""The jq budget kills a runaway evaluation in its worker process while the caller's loop keeps serving."""

import asyncio
import time

import pytest

pytest.importorskip("jq")

from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.data.jq_errors import JqBudgetExceededError
from tai42_kit.utils.data.jq_util import run_jq_bounded, run_jq_first

_TICK = 0.01
_NEVER_ENDS = "last(range(1e18))"


async def _tick_ratio(job) -> float:
    """Await ``job`` (expected to exceed its jq budget) while a heartbeat ticks; return the delivered tick ratio."""
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(_TICK)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        with pytest.raises(JqBudgetExceededError, match="JQ_TIMEOUT_SECONDS"):
            await job
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        await beat
    assert elapsed < 1.0, f"the budget answered after {elapsed:.2f}s"
    return ticks / (elapsed / _TICK)


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.2")
    reset_all_settings()
    yield
    reset_all_settings()


async def test_a_runaway_first_evaluation_times_out_while_the_loop_ticks(short_budget) -> None:
    assert await run_jq_first(".a", {"a": 1}) == 1

    ratio = await _tick_ratio(run_jq_first(_NEVER_ENDS, None))

    assert ratio >= 0.8
    assert await run_jq_first(".a", {"a": 2}) == 2


async def test_a_runaway_bounded_evaluation_times_out_while_the_loop_ticks(short_budget) -> None:
    assert await run_jq_bounded(".[]", [1], limit=1) == [1]

    ratio = await _tick_ratio(run_jq_bounded(_NEVER_ENDS, None, limit=1))

    assert ratio >= 0.8
    assert await run_jq_bounded(".[]", [3, 4], limit=2) == [3, 4]
