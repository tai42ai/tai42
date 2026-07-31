"""Tests for the refcounted per-``session_key`` guard in the ``request`` tool.

The guard serializes concurrent same-key requests (so a pooled session's shared RESOLVE pin is
never clobbered) while evicting each key's entry once no holder or waiter references it.
"""

from __future__ import annotations

import asyncio

from tai42_toolbox._internal.tools import http_client


def test_distinct_key_sequential_guards_leave_map_empty() -> None:
    # Each distinct key gets its own entry while held and is evicted on release, so after N
    # sequential guards the loop's inner map is empty — no per-key growth.
    async def scenario() -> None:
        for key in ("a", "b", "c", "d", "e"):
            async with http_client._session_guard(key):
                pass
        loop = asyncio.get_running_loop()
        assert http_client._session_locks.get(loop) == {}

    asyncio.run(scenario())


def test_concurrent_same_key_guards_serialize_and_evict() -> None:
    # Two concurrent same-key guards run strictly one-at-a-time (never
    # interleaved), and once both complete the shared entry is gone.
    async def scenario() -> None:
        order: list[str] = []
        first_in = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with http_client._session_guard("k"):
                order.append("enter-1")
                first_in.set()
                await release_first.wait()
                order.append("exit-1")

        async def second() -> None:
            await first_in.wait()
            async with http_client._session_guard("k"):
                order.append("enter-2")
                order.append("exit-2")

        t1 = asyncio.ensure_future(first())
        t2 = asyncio.ensure_future(second())
        await first_in.wait()
        # Let the second worker reach the guard and block on the shared lock
        # before the first releases it.
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(t1, t2)

        assert order == ["enter-1", "exit-1", "enter-2", "exit-2"]
        loop = asyncio.get_running_loop()
        assert http_client._session_locks.get(loop) == {}

    asyncio.run(scenario())


def test_waiter_reuses_the_same_lock_object() -> None:
    # A waiter arriving while a holder is inside references the SAME entry (refcount 2) and the SAME lock.
    async def scenario() -> None:
        holder_in = asyncio.Event()
        release = asyncio.Event()
        captured: dict[str, object] = {}

        async def holder() -> None:
            async with http_client._session_guard("k"):
                loop = asyncio.get_running_loop()
                captured["holder_lock"] = http_client._session_locks[loop]["k"][0]
                holder_in.set()
                await release.wait()

        async def waiter() -> None:
            async with http_client._session_guard("k"):
                loop = asyncio.get_running_loop()
                captured["waiter_lock"] = http_client._session_locks[loop]["k"][0]

        t_holder = asyncio.ensure_future(holder())
        await holder_in.wait()
        t_waiter = asyncio.ensure_future(waiter())
        # One scheduling turn lets the waiter enter the guard, increment the
        # refcount, and block on the still-held lock.
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        assert http_client._session_locks[loop]["k"][1] == 2
        release.set()
        await asyncio.gather(t_holder, t_waiter)

        assert captured["holder_lock"] is captured["waiter_lock"]
        assert http_client._session_locks.get(loop) == {}

    asyncio.run(scenario())
