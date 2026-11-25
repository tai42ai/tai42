"""SingleFlight: one read per key while in flight, shared by every caller, cancelled only with its last caller."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.template.single_flight import SingleFlight


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
    flights: SingleFlight[str] = SingleFlight()
    reads = _Reads()
    callers = [asyncio.create_task(flights.run("k", reads.read)) for _ in range(3)]
    await _settle()
    reads.release.set()
    assert await asyncio.gather(*callers) == ["text", "text", "text"]
    assert reads.started == 1
    assert await flights.run("k", reads.read) == "text"
    assert reads.started == 2


async def test_every_caller_sees_the_failure_of_the_shared_read() -> None:
    flights: SingleFlight[str] = SingleFlight()

    async def failing() -> str:
        await asyncio.sleep(0)
        raise ConnectionError("down")

    results = await asyncio.gather(flights.run("k", failing), flights.run("k", failing), return_exceptions=True)
    assert [type(result) for result in results] == [ConnectionError, ConnectionError]
    assert results[0] is results[1]


async def test_a_cancelled_caller_leaves_the_read_to_the_others_and_the_last_cancels_it() -> None:
    flights: SingleFlight[str] = SingleFlight()
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


async def _fresh_read(flights: SingleFlight[str], reads: _Reads) -> str:
    reads.release.set()
    return await flights.run("k", reads.read)


async def test_a_detached_read_answers_its_callers_and_the_next_caller_reads_again() -> None:
    flights: SingleFlight[str] = SingleFlight()
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
