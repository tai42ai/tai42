"""The bounded read of an exec's output, :func:`receive_within`."""

from __future__ import annotations

import asyncio

import pytest

from tai42_kit.sandbox.exec_deadline import receive_within


@pytest.mark.parametrize("remaining", [0.0, -1.0])
async def test_a_passed_bound_still_returns_an_item_already_held(remaining: float) -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("held")
    assert await receive_within(queue.get, remaining, ()) == (True, "held")
    assert queue.empty()


async def test_a_passed_bound_with_nothing_held_returns_at_once_and_takes_nothing() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await receive_within(queue.get, 0.0, ()) == (False, None)
    assert loop.time() - started < 0.5
    # The cancelled receive took nothing: an item queued after it is still there.
    queue.put_nowait("later")
    assert queue.get_nowait() == "later"


async def test_a_bound_that_passes_with_nothing_received_returns_nothing() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    assert await receive_within(queue.get, 0.02, ()) == (False, None)


async def test_a_wake_ends_the_wait_before_the_bound() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    wake = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, wake.set)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await receive_within(queue.get, 10.0, (wake,)) == (False, None)
    assert loop.time() - started < 5.0


async def test_a_wake_already_set_is_not_waited_on() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    wake = asyncio.Event()
    wake.set()
    asyncio.get_running_loop().call_later(0.02, queue.put_nowait, "item")
    assert await receive_within(queue.get, 10.0, (wake,)) == (True, "item")
