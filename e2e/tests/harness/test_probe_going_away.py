"""Harness self-tests for the boot readiness probe on a worker recycled while the fleet boots.

A supervised serve worker can recycle itself while the fleet boots (a worker that joined the bus
names a key it runs a stored copy of). Its graceful stop closes its listener first and drains
before the process exits, so a probe call can fail with a refused connection while the process
still runs. The probe (:func:`_unless_its_process_exits`) reads that as a process not up to
answer — the enclosing wait polls again — and still raises a call failure from a process that
keeps answering ``/health``. A stand-in process and a loopback ``/health`` server drive it, no
infra.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import httpx
import pytest

from tai42_e2e.readiness import _unless_its_process_exits
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("no-stack")

_HEALTH_OK = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok"


@dataclass
class _Running:
    """A process that keeps running for the whole probe, as a worker in its graceful stop does."""

    name: str

    def is_running(self) -> bool:
        return True


async def _health_server() -> asyncio.Server:
    """A loopback server answering every request ``200``, as a serving worker's ``/health`` does."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(_HEALTH_OK)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(answer, "127.0.0.1", 0)


def _stack(port: int) -> TaiStack:
    """A bare stack whose one serve process, still running, serves ``port``."""
    stack = object.__new__(TaiStack)
    handle = _Running("serve")
    stack._procs = {"serve": handle}  # type: ignore[attr-defined]
    stack._supervisor_lock = threading.RLock()  # type: ignore[attr-defined]
    stack._supervised_names = {"serve"}  # type: ignore[attr-defined]
    stack.process_on_port = lambda _port: handle  # type: ignore[method-assign,assignment,return-value]
    stack.base_url = lambda _port=None: f"http://127.0.0.1:{port}"  # type: ignore[method-assign]
    return stack


async def _refused(port: int) -> None:
    """The failure the MCP client raises when the server's listener is closed."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"http://127.0.0.1:{port}/mcp")
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Client failed to connect: {exc}") from exc
    raise AssertionError("the closed listener answered")


async def test_a_call_refused_by_a_process_that_closed_its_listener_is_not_ready_yet() -> None:
    server = await _health_server()
    port = server.sockets[0].getsockname()[1]

    async def call() -> str:
        # The worker starts its graceful stop under the call: its listener closes, the process runs on.
        server.close()
        await server.wait_closed()
        await _refused(port)
        return "answered"

    assert await _unless_its_process_exits(_stack(port), port, call) is None


async def test_a_call_that_fails_while_its_process_keeps_answering_raises() -> None:
    server = await _health_server()
    port = server.sockets[0].getsockname()[1]

    async def call() -> str:
        raise RuntimeError("the tool call failed")

    try:
        with pytest.raises(RuntimeError, match="the tool call failed"):
            await _unless_its_process_exits(_stack(port), port, call)
    finally:
        server.close()
        await server.wait_closed()


async def test_a_call_its_process_answers_returns_the_answer() -> None:
    server = await _health_server()
    port = server.sockets[0].getsockname()[1]

    async def call() -> str:
        return "answered"

    try:
        assert await _unless_its_process_exits(_stack(port), port, call) == "answered"
    finally:
        server.close()
        await server.wait_closed()
