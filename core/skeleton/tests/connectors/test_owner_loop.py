"""The serving loop's stand-in ends every task left on it before it closes, as ``asyncio.run`` does."""

from __future__ import annotations

import asyncio
import threading

import pytest
from tai42_kit.process_lock import ProcessLock

from .credential_path import owner_loop


def test_a_task_still_holding_a_process_lock_is_cancelled_and_releases_it_before_the_loop_closes():
    lock = ProcessLock()
    taken = threading.Event()

    async def hold() -> None:
        async with lock.held():
            taken.set()
            await asyncio.Event().wait()

    with owner_loop() as loop:
        holder = asyncio.run_coroutine_threadsafe(hold(), loop)
        assert taken.wait(5)
    assert holder.cancelled()
    assert not lock.locked


def test_a_task_that_fails_while_it_is_cancelled_fails_the_teardown():
    started = threading.Event()

    async def fail_on_cancel() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    serving = owner_loop()
    loop = serving.__enter__()
    asyncio.run_coroutine_threadsafe(fail_on_cancel(), loop)
    assert started.wait(5)
    with pytest.raises(BaseExceptionGroup) as caught:
        serving.__exit__(None, None, None)
    [error] = caught.value.exceptions
    assert isinstance(error, RuntimeError)
    assert str(error) == "cleanup failed"
