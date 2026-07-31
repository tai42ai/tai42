"""Live verification: fleet-op delivery under load, against a real Redis and a
real RQ prefork worker.

``run_rq_worker`` runs the blocking work loop on a worker thread, so the app's
worker-bus subscription keeps applying fleet ops on this process's loop while jobs
run; such an apply is inherited by the next job's forked work-horse child. This
test mutates a main-process registry from a loop coroutine while the work loop
runs (mutating it at all proves the loop is not starved), then asserts the next
job's real ``os.fork()`` child reads the mutated registry.

Skipped when no Redis is reachable — set ``TAI_TEST_RQ_REDIS``
(default ``redis://localhost:6379/15``) to run it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from typing import cast

import pytest
from redis import Redis
from rq import Queue

from tai42_backend_rq.worker import run_rq_worker

# The registry a fleet op would mutate in the worker's MAIN process. A work-horse
# child inherits its value AT FORK, so the value a job observes is the one the
# main process held when that job's child was forked.
_APPLIED_REGISTRY = "registry-v0"

_REDIS_URL = os.environ.get("TAI_TEST_RQ_REDIS", "redis://localhost:6379/15")


def _redis_reachable(url: str) -> bool:
    try:
        Redis.from_url(url).ping()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _redis_reachable(_REDIS_URL), reason=f"no Redis reachable at {_REDIS_URL}")


def observe_registry(list_key: str) -> str:
    """RQ job body (runs in a forked work-horse): record the registry value this
    child inherited by pushing it onto ``list_key``, using its own post-fork Redis
    connection."""
    conn = Redis.from_url(_REDIS_URL)
    try:
        conn.rpush(list_key, _APPLIED_REGISTRY)
    finally:
        conn.close()
    return _APPLIED_REGISTRY


async def _await_list(conn: Redis, list_key: str, length: int, timeout: float = 45.0) -> list[str]:
    async with asyncio.timeout(timeout):
        while True:
            raw = cast("list[bytes]", conn.lrange(list_key, 0, -1))
            values = [v.decode() for v in raw]
            if len(values) >= length:
                return values
            # Yielding here is the responsiveness probe: this coroutine only makes
            # progress if the event loop is not starved by the blocking work loop
            # running on its own thread.
            await asyncio.sleep(0.05)


async def test_next_task_forked_child_sees_a_main_process_apply() -> None:
    global _APPLIED_REGISTRY

    conn = Redis.from_url(_REDIS_URL)
    list_key = f"tai-live-verify:{uuid.uuid4().hex}"
    queue = Queue(connection=conn)

    conn.delete(list_key)
    _APPLIED_REGISTRY = "registry-v0"

    worker_task = asyncio.create_task(
        run_rq_worker(_REDIS_URL, f"live-verify-{uuid.uuid4().hex}", "ERROR", False, 500, "prefork")
    )
    try:
        # First job: its forked child reads the initial registry.
        queue.enqueue(observe_registry, list_key)
        first = await _await_list(conn, list_key, 1)
        assert first == ["registry-v0"]
        assert not worker_task.done(), "the work loop must still be running (off-thread)"

        # Apply an op in the MAIN worker process (this pytest process is the worker
        # process — the work loop runs on a thread of it), exactly where the bus
        # subscription would apply it on the responsive loop.
        _APPLIED_REGISTRY = "registry-v1"

        # Next job: its child is forked AFTER the apply, so it must inherit it.
        queue.enqueue(observe_registry, list_key)
        both = await _await_list(conn, list_key, 2)
        assert both == ["registry-v0", "registry-v1"], "the next task's forked child did not see the main-process apply"
    finally:
        _APPLIED_REGISTRY = "registry-v0"
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        conn.delete(list_key)
        conn.close()
