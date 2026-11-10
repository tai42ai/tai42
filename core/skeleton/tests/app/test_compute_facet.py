"""``tai42_app.compute`` reached by a tool of the served app: the job runs in a worker, the loop keeps serving.

A neutral consumer: a test-local tool hands a module-level job to ``tai42_app.compute.run``. While
a job that holds its interpreter lock runs, the serving loop delivers its heartbeat and answers
``GET /health``; the job's exception and the timeout reach the tool's caller.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable

import httpx
import pytest

from tai42_skeleton.app.instance import app

from ._served_app import REQUEST_TIMEOUT_S, served

COMPUTE_MANIFEST = {
    "default_routers": "none",
    "routers_modules": ["tai42_skeleton.routers.health"],
    "tools": [{"title": "compute", "module": "tests.app._fixtures.compute_tools", "include": ["compute_probe"]}],
}
# A subject this long keeps the backtracking job busy for about a second on a current core.
BACKTRACK_LENGTH = 25
TICK = 0.02


async def _tick_ratio[T](job: Awaitable[T]) -> tuple[T, float]:
    """Await ``job`` while a heartbeat ticks every ``TICK`` seconds; return its result and the tick ratio."""
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(TICK)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        result = await job
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        await beat
    return result, ticks / (elapsed / TICK)


async def _probe(job: str, arg: int, timeout: float | None = None) -> object:
    return await app.tools.run_tool("compute_probe", {"job": job, "arg": arg, "timeout": timeout})


async def test_a_gil_holding_job_leaves_the_serving_loop_ticking(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, COMPUTE_MANIFEST):
        await _probe("backtrack", 10)  # the worker has imported the job's module

        matched, ratio = await _tick_ratio(_probe("backtrack", BACKTRACK_LENGTH))

    assert matched is False
    assert ratio >= 0.8


async def test_health_answers_within_100_ms_while_a_compute_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        served(monkeypatch, COMPUTE_MANIFEST) as server,
        httpx.AsyncClient(base_url=server.base_url, timeout=REQUEST_TIMEOUT_S) as client,
    ):
        await _probe("backtrack", 10)
        assert (await client.get("/health")).status_code == 200  # the connection is open

        job = asyncio.create_task(_probe("backtrack", BACKTRACK_LENGTH + 1))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        response = await client.get("/health")
        answered_in = time.monotonic() - started
        still_running = not job.done()
        assert await job is False

    assert response.status_code == 200
    assert response.text == "OK"
    assert still_running
    assert answered_in < 0.1


async def test_the_jobs_exception_reaches_the_tools_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, COMPUTE_MANIFEST):
        with pytest.raises(ValueError, match="the job refused input 0"):
            await app.tools.run_tool("compute_probe", {"job": "refuse", "arg": 0})


async def test_a_timeout_terminates_the_job_and_the_next_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, COMPUTE_MANIFEST):
        with pytest.raises(TimeoutError, match="its worker was terminated"):
            await _probe("linger", 30, timeout=0.3)

        assert await _probe("backtrack", 10) is False
