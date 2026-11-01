"""A one-shot exec's stdin is fed while its output is read, and the first of the two to fail raises."""

from __future__ import annotations

import asyncio

import pytest

from tai42_kit.sandbox import read_while_feeding


class _Pipe:
    """A pipe of ``capacity`` items: a put waits while it is full, as a command's output does."""

    def __init__(self, capacity: int) -> None:
        self.items: asyncio.Queue[int | None] = asyncio.Queue(maxsize=capacity)
        self.read: list[int] = []

    async def feed(self) -> None:
        for item in range(10):
            await self.items.put(item)
        await self.items.put(None)

    async def drain(self) -> None:
        while (item := await self.items.get()) is not None:
            self.read.append(item)


async def test_the_feed_and_the_read_run_together_so_a_full_pipe_never_blocks_the_feed() -> None:
    pipe = _Pipe(capacity=2)
    async with asyncio.timeout(5):
        await read_while_feeding(pipe.drain, pipe.feed)
    assert pipe.read == list(range(10))


async def test_with_no_feed_the_read_runs_alone() -> None:
    pipe = _Pipe(capacity=1)
    pipe.items.put_nowait(None)
    await read_while_feeding(pipe.drain, None)
    assert pipe.read == []


async def test_a_failed_feed_raises_and_cancels_the_read() -> None:
    read_cancelled = asyncio.Event()

    async def read() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            read_cancelled.set()
            raise

    async def feed() -> None:
        raise ConnectionResetError("stdin lost")

    with pytest.raises(ConnectionResetError, match=r"^stdin lost$"):
        await read_while_feeding(read, feed)
    assert read_cancelled.is_set()


async def test_a_failed_read_raises_and_cancels_the_feed() -> None:
    feed_cancelled = asyncio.Event()

    async def read() -> None:
        raise RuntimeError("output lost")

    async def feed() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            feed_cancelled.set()
            raise

    with pytest.raises(RuntimeError, match=r"^output lost$"):
        await read_while_feeding(read, feed)
    assert feed_cancelled.is_set()


async def test_a_read_that_ends_first_still_waits_for_the_feed_and_its_failure() -> None:
    async def read() -> None:
        return None

    async def feed() -> None:
        await asyncio.sleep(0.05)
        raise ConnectionResetError("stdin lost")

    with pytest.raises(ConnectionResetError, match=r"^stdin lost$"):
        await read_while_feeding(read, feed)


async def test_when_both_fail_the_first_raises_with_the_other_noted() -> None:
    async def read() -> None:
        raise RuntimeError("output lost")

    async def feed() -> None:
        raise ConnectionResetError("stdin lost")

    with pytest.raises(ConnectionResetError) as raised:
        await read_while_feeding(read, feed)
    assert str(raised.value) == "stdin lost"
    assert raised.value.__notes__ == ["the output read also failed: RuntimeError('output lost')"]


async def test_a_cancelled_call_cancels_the_feed_and_the_read() -> None:
    cancelled: list[str] = []

    def waiter(name: str):
        async def wait() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return wait

    call = asyncio.ensure_future(read_while_feeding(waiter("read"), waiter("feed")))
    await asyncio.sleep(0.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert sorted(cancelled) == ["feed", "read"]
