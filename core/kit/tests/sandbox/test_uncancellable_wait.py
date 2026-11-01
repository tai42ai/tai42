"""A wait on cleanup work runs the work to its end through every cancellation of the caller."""

from __future__ import annotations

import asyncio

import anyio

from tai42_kit.sandbox.uncancellable_wait import wait_through_cancellation


class _Work:
    """Work that takes several wake-ups to finish, as a cleanup's API round trips do."""

    def __init__(self, steps: int) -> None:
        self.steps = steps
        self.done_steps = 0
        self.started = asyncio.Event()

    async def run(self) -> str:
        self.started.set()
        for _ in range(self.steps):
            await asyncio.sleep(0)
            self.done_steps += 1
        return "finished"


async def test_a_wait_no_cancellation_reaches_returns_none_once_the_work_ended() -> None:
    work = _Work(steps=3)
    task = asyncio.ensure_future(work.run())

    assert await wait_through_cancellation(task) is None
    assert task.result() == "finished"


async def test_a_wait_does_not_raise_the_work_s_own_error() -> None:
    async def _fails() -> None:
        raise RuntimeError("delete failed")

    task = asyncio.ensure_future(_fails())

    assert await wait_through_cancellation(task) is None
    assert isinstance(task.exception(), RuntimeError)


async def test_a_wait_an_anyio_scope_cancels_runs_the_work_to_its_end_and_returns_the_cancellation() -> None:
    work = _Work(steps=5)
    task = asyncio.ensure_future(work.run())
    outcome: list[asyncio.CancelledError | None] = []

    with anyio.CancelScope() as scope:
        scope.cancel()
        cancelled = await wait_through_cancellation(task)
        outcome.append(cancelled)
        if cancelled is not None:
            # The caller re-raises the cancellation once it read the work's outcome.
            raise cancelled

    assert scope.cancelled_caught
    [cancelled] = outcome
    assert isinstance(cancelled, asyncio.CancelledError)
    assert task.result() == "finished"
    assert work.done_steps == 5


async def test_a_wait_a_plain_cancel_reaches_again_and_again_still_runs_the_work_to_its_end() -> None:
    release = asyncio.Event()

    async def _held() -> str:
        await release.wait()
        return "finished"

    task = asyncio.ensure_future(_held())
    waiter = asyncio.ensure_future(wait_through_cancellation(task))
    await asyncio.sleep(0)
    for _ in range(3):
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done()
    release.set()

    cancelled = await waiter
    assert isinstance(cancelled, asyncio.CancelledError)
    assert task.result() == "finished"


async def test_a_wait_never_cancels_the_work_it_waits_on() -> None:
    work = _Work(steps=2)
    task = asyncio.ensure_future(work.run())
    waiter = asyncio.ensure_future(wait_through_cancellation(task))
    await work.started.wait()
    waiter.cancel()

    await waiter
    assert not task.cancelled()
    assert task.result() == "finished"
