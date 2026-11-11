"""A compute pool reached over a socket: the serving end and the client end on one loop."""

from __future__ import annotations

import asyncio
import logging
import operator
import os
import socket
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest

from tai42_kit.compute import ComputePool, ComputePoolClient, ComputePoolUnavailableError, pool_wire
from tai42_kit.compute.pool_wire import PoolServer
from tai42_kit.compute.wire_sockets import wire_sockets

from .descriptor_limit import at_descriptor_limit

_NO_RESULT = (
    r"^the process holding the compute pool gave no result for time\.sleep: "
    r"it ended, or it could not take or serve the job$"
)


@pytest.fixture
def pool() -> Iterator[ComputePool]:
    pool = ComputePool(1)
    pool.start()
    yield pool
    pool.stop()


@dataclass
class _Served:
    client: ComputePoolClient
    serving: asyncio.Task[None]


@pytest.fixture
async def served(pool: ComputePool) -> AsyncIterator[_Served]:
    serving_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    server = PoolServer(serving_end, lambda: pool)
    accepting = asyncio.Event()
    serving = asyncio.ensure_future(server.serve(on_serving=accepting.set))
    await asyncio.wait_for(accepting.wait(), 5)
    yield _Served(ComputePoolClient(client_end), serving)
    serving.cancel()
    await asyncio.wait({serving})
    server.close()
    client_end.close()


async def test_a_job_sent_by_the_client_returns_its_result(served: _Served) -> None:
    assert await served.client.run(operator.add, 20, 22, timeout=30) == 42


async def test_serving_ends_when_the_client_closes_the_rendezvous(served: _Served) -> None:
    served.client.close()

    await asyncio.wait_for(asyncio.wait({served.serving}), 5)

    assert served.serving.result() is None


async def test_a_job_after_the_client_closed_raises_that_the_pool_is_gone(served: _Served) -> None:
    served.client.close()

    with pytest.raises(ComputePoolUnavailableError, match="the process holding the compute pool is gone; "):
        await served.client.run(operator.add, 1, 1)


async def test_a_job_in_flight_when_serving_is_cancelled_raises_that_it_got_no_result(served: _Served) -> None:
    job = asyncio.ensure_future(served.client.run(time.sleep, 30))
    await asyncio.sleep(0.5)

    served.serving.cancel()

    with pytest.raises(ComputePoolUnavailableError, match=_NO_RESULT):
        await asyncio.wait_for(job, 10)


async def test_a_client_that_ran_a_job_records_no_end_of_its_server(served: _Served) -> None:
    await served.client.run(operator.add, 1, 1, timeout=30)

    assert served.client.server_ended is False


async def test_a_job_whose_socket_the_server_closes_with_no_reply_raises_and_records_no_end_of_its_server(
    served: _Served,
) -> None:
    job = asyncio.ensure_future(served.client.run(time.sleep, 30))
    await asyncio.sleep(0.5)
    served.serving.cancel()

    with pytest.raises(ComputePoolUnavailableError, match=_NO_RESULT) as raised:
        await asyncio.wait_for(job, 10)

    assert not isinstance(raised.value, pool_wire.JobNotSentError)
    assert served.client.server_ended is False


async def test_a_job_whose_end_the_servers_receive_lost_raises_and_the_server_logs_it_and_serves_the_next_job(
    served: _Served, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    receive = wire_sockets.received
    lost: list[bool] = []

    def received_with_no_descriptor_free_once(end: socket.socket) -> tuple[bytes, list[socket.socket]]:
        if lost:
            return receive(end)
        lost.append(True)
        with at_descriptor_limit():
            return receive(end)

    monkeypatch.setattr(wire_sockets, "received", received_with_no_descriptor_free_once)

    with (
        caplog.at_level(logging.ERROR, logger=pool_wire.__name__),
        pytest.raises(ComputePoolUnavailableError, match=_NO_RESULT) as raised,
    ):
        await asyncio.wait_for(served.client.run(time.sleep, 30, timeout=30), 10)
    later = await asyncio.wait_for(served.client.run(operator.add, 20, 22, timeout=30), 10)

    assert not isinstance(raised.value, pool_wire.JobNotSentError)
    assert later == 42
    assert served.client.server_ended is False
    assert served.serving.done() is False
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (
            logging.ERROR,
            "a compute job was not taken: this process has no file descriptor free for its socket; its caller raises",
        )
    ]


async def test_a_job_sent_to_a_closed_server_end_raises_that_it_was_not_sent_and_records_the_end() -> None:
    serving_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    serving_end.close()
    client = ComputePoolClient(client_end)
    try:
        with pytest.raises(
            pool_wire.JobNotSentError, match=r"the process holding the compute pool is gone; _?operator\.add cannot run"
        ):
            await client.run(operator.add, 1, 1)
    finally:
        client.close()

    assert client.server_ended is True


async def test_a_job_after_the_client_closed_records_no_end_of_its_server(served: _Served) -> None:
    served.client.close()

    with pytest.raises(ComputePoolUnavailableError) as raised:
        await served.client.run(operator.add, 1, 1)

    assert not isinstance(raised.value, pool_wire.JobNotSentError)
    assert served.client.server_ended is False


async def test_a_job_sent_while_this_process_has_no_descriptor_free_raises_and_leaves_nothing_and_the_next_job_runs(
    served: _Served,
) -> None:
    opened = len(os.listdir("/proc/self/fd"))
    recorded = set(wire_sockets._held)

    with (
        pytest.raises(
            ComputePoolUnavailableError,
            match=r"^this process could not make the socket for _?operator\.add; _?operator\.add cannot run$",
        ) as raised,
        at_descriptor_limit(),
    ):
        await served.client.run(operator.add, 1, 1, timeout=30)

    assert isinstance(raised.value.__cause__, OSError)
    assert not isinstance(raised.value, pool_wire.JobNotSentError)
    assert served.client.server_ended is False
    assert len(os.listdir("/proc/self/fd")) == opened
    assert set(wire_sockets._held) == recorded
    assert await asyncio.wait_for(served.client.run(operator.add, 20, 22, timeout=30), 10) == 42
