"""A retiring generation's periodic loops: each cancel is bounded by the drain budget, and an overrun is reported."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from tai42_kit.clients.base import current_client_epoch

from tai42_skeleton.app.epoch import Epoch


async def test_a_loop_that_outlives_its_cancel_budget_is_reported_even_when_its_cancel_swallows_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registered cancel cancels its loop task and awaits it, suppressing the task's own
    ``CancelledError``; a loop whose unwind is slow (blocked on a call that does not yield to
    the cancellation) outlives the budget, and the retire reports it and moves on."""
    ep = Epoch(number=current_client_epoch())
    unblock = asyncio.Event()

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await unblock.wait()  # the slow unwind
                raise

    task = asyncio.create_task(_loop())
    await asyncio.sleep(0)

    async def _cancel() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    ep.register_periodic_loop(_cancel)
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        with caplog.at_level(logging.ERROR):
            await ep._cancel_periodic_loops(0.05)
        assert loop.time() - start < 1.0, "the retire waited past the budget on the loop's unwind"
        assert "a periodic-loop cancel exceeded the 0.1s budget; abandoning it" in caplog.text
    finally:
        unblock.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_cancel_that_fails_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    ep = Epoch(number=current_client_epoch())

    async def _failing_cancel() -> None:
        raise RuntimeError("the loop's client refused to close")

    ep.register_periodic_loop(_failing_cancel)
    with caplog.at_level(logging.ERROR):
        await ep._cancel_periodic_loops(0.05)
    assert "a periodic-loop cancel failed" in caplog.text
    assert "the loop's client refused to close" in caplog.text
