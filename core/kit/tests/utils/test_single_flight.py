"""SingleFlight: one read per key while in flight, shared by every caller, cancelled only with its last caller.

Keys are any hashable value; most cases here use names, the last a tuple.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tai42_kit.utils.single_flight import SingleFlight


class _Reads:
    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0
        self.release = asyncio.Event()

    async def read(self) -> str:
        self.started += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return "text"


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_callers_of_one_key_in_flight_share_one_read_and_the_next_caller_reads_again() -> None:
    flights: SingleFlight[str, str] = SingleFlight()
    reads = _Reads()
    callers = [asyncio.create_task(flights.run("k", reads.read)) for _ in range(3)]
    await _settle()
    reads.release.set()
    assert await asyncio.gather(*callers) == ["text", "text", "text"]
    assert reads.started == 1
    assert await flights.run("k", reads.read) == "text"
    assert reads.started == 2


async def test_every_caller_sees_the_failure_of_the_shared_read() -> None:
    flights: SingleFlight[str, str] = SingleFlight()

    async def failing() -> str:
        await asyncio.sleep(0)
        raise ConnectionError("down")

    results = await asyncio.gather(flights.run("k", failing), flights.run("k", failing), return_exceptions=True)
    assert [type(result) for result in results] == [ConnectionError, ConnectionError]
    assert results[0] is results[1]


async def test_a_cancelled_caller_leaves_the_read_to_the_others_and_the_last_cancels_it() -> None:
    flights: SingleFlight[str, str] = SingleFlight()
    reads = _Reads()
    first = asyncio.create_task(flights.run("k", reads.read))
    second = asyncio.create_task(flights.run("k", reads.read))
    await _settle()
    first.cancel()
    await _settle()
    assert reads.cancelled == 0
    second.cancel()
    await _settle()
    assert reads.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await second
    assert await asyncio.wait_for(_fresh_read(flights, reads), 1) == "text"
    assert reads.started == 2


async def _fresh_read(flights: SingleFlight[str, str], reads: _Reads) -> str:
    reads.release.set()
    return await flights.run("k", reads.read)


async def test_a_detached_read_answers_its_callers_and_the_next_caller_reads_again() -> None:
    flights: SingleFlight[str, str] = SingleFlight()
    reads = _Reads()
    first = asyncio.create_task(flights.run("lib/a.j2", reads.read))
    other = asyncio.create_task(flights.run("page.j2", reads.read))
    await _settle()
    flights.detach(lambda key: key.startswith("lib/"))
    second = asyncio.create_task(flights.run("lib/a.j2", reads.read))
    await _settle()
    assert reads.started == 3
    reads.release.set()
    assert await asyncio.gather(first, other, second) == ["text", "text", "text"]


async def test_a_tuple_key_shares_one_read_and_detaches_by_its_parts() -> None:
    flights: SingleFlight[tuple[str, int], str] = SingleFlight()
    reads = _Reads()
    first = asyncio.create_task(flights.run(("u1", 3), reads.read))
    joined = asyncio.create_task(flights.run(("u1", 3), reads.read))
    other_version = asyncio.create_task(flights.run(("u1", 4), reads.read))
    await _settle()
    assert reads.started == 2
    flights.detach(lambda key: key[1] == 3)
    fresh = asyncio.create_task(flights.run(("u1", 3), reads.read))
    await _settle()
    assert reads.started == 3
    reads.release.set()
    assert await asyncio.gather(first, joined, other_version, fresh) == ["text"] * 4


def test_a_caller_on_another_loop_starts_its_own_read() -> None:
    flights: SingleFlight[str, str] = SingleFlight()
    first_started = threading.Event()
    second_called = threading.Event()
    started: list[str] = []

    async def read() -> str:
        started.append("k")
        first_started.set()
        # Held off the loop thread until the second loop has called, so that call lands mid-flight.
        assert await asyncio.to_thread(second_called.wait, 10)
        return "text"

    async def first() -> str:
        return await flights.run("k", read)

    async def second() -> str:
        second_called.set()
        return await flights.run("k", read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(asyncio.run, first())
        assert first_started.wait(10)
        second_result = pool.submit(asyncio.run, second())

        assert second_result.result(timeout=10) == "text"
        assert first_result.result(timeout=10) == "text"
    assert started == ["k", "k"]
