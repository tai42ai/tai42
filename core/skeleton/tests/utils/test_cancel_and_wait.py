"""``cancel_and_wait``: stopping a task, and what a cancellation of the caller does meanwhile."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.utils.tasks import cancel_and_wait


async def test_a_task_ended_by_the_cancel_returns_quietly() -> None:
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)

    await cancel_and_wait(task)

    assert task.cancelled()


async def test_a_task_that_ends_with_an_error_on_its_cancel_raises_that_error() -> None:
    async def fails_on_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    task = asyncio.create_task(fails_on_cancel())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await cancel_and_wait(task)


async def test_a_task_already_done_is_only_waited_on() -> None:
    task = asyncio.create_task(asyncio.sleep(0, result=None))
    await task

    await cancel_and_wait(task)

    assert not task.cancelled()


async def test_a_cancellation_of_the_caller_while_the_task_winds_down_propagates() -> None:
    winding_down = asyncio.Event()
    wound_down = asyncio.Event()

    async def slow_to_end() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            winding_down.set()
            await wound_down.wait()
            raise

    task = asyncio.create_task(slow_to_end())
    await asyncio.sleep(0)
    caller = asyncio.create_task(cancel_and_wait(task))
    await winding_down.wait()

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert not task.done()
    wound_down.set()
    with pytest.raises(asyncio.CancelledError):
        await task
