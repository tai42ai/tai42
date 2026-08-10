"""Live verification: a real prefork worker's forked child re-inherits the
parent's mutated tool registry only after the ``on_fleet_op_applied`` successor
confirms pool turnover.

Runs a genuine Celery prefork worker (real ``fork`` / ``pool_restart`` / ``stats``
turnover) against a Redis broker, applies a mutating op in the parent, drives the
successor's entrypoint, and asserts through a pool child (``probe_registry``
returns the child pid + the registry it was forked with).

Skipped when no Redis broker is reachable — set ``TAI_TEST_CELERY_BROKER``
(default ``redis://localhost:6399/0``) to run it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

_BROKER = os.environ.get("TAI_TEST_CELERY_BROKER", "redis://localhost:6399/0")


def _redis_reachable(url: str) -> bool:
    import redis

    try:
        redis.Redis.from_url(url).ping()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.skipif(not _redis_reachable(_BROKER), reason=f"no Redis broker reachable at {_BROKER}"),
    # A live worker boot legitimately emits framework warnings; do not treat them
    # as errors for this one integration test.
    pytest.mark.filterwarnings("ignore"),
]


def _submit(celery_app: Any) -> list[Any]:
    """Run the probe in a pool child and return ``[child_pid, sorted(tools)]``."""
    # ``disable_sync_subtasks=False``: this process hosts a worker, which arms
    # Celery's "never call get() in a task" guard; the test driver is not a task.
    return celery_app.send_task("tests.probe_registry").get(timeout=30, disable_sync_subtasks=False)


def _wait_worker_ready(prefork: Any, celery_app: Any, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if prefork._worker_ready.is_set() and prefork._local_nodename is not None:
            pong = celery_app.control.ping(timeout=1.0)
            if pong:
                return
        time.sleep(0.2)
    raise RuntimeError("live celery worker did not become ready in time")


@pytest.fixture
def live_worker() -> Iterator[Any]:
    """Boot a real prefork worker (concurrency 1) in a background thread, wired to
    the Redis broker, with the prefork-turnover signals connected. Torn down by
    requesting worker shutdown and reaping any surviving pool child."""
    import tests._live_probe as probe  # registers the probe task on celery_app
    from tai42_backend_celery.core import prefork
    from tai42_backend_celery.core.app import celery_app

    celery_app.conf.update(broker_url=_BROKER, result_backend=_BROKER)

    # Wire the successor's signals (node-name capture + readiness) and its lifecycle
    # handler, exactly as launch does.
    prefork._local_nodename = None
    prefork._worker_ready.clear()
    prefork.register()

    worker = celery_app.Worker(
        pool="prefork",
        concurrency=1,
        without_gossip=True,
        without_mingle=True,
        without_heartbeat=True,
        loglevel="ERROR",
        quiet=True,
    )
    thread = threading.Thread(target=worker.start, name="live-celery-worker", daemon=True)
    thread.start()
    try:
        _wait_worker_ready(prefork, celery_app)
        yield probe
    finally:
        from celery import _state
        from celery.worker import state

        # Reap any pool child before stopping (the stats read needs the worker
        # still consuming), so the suite does not leak forked processes.
        try:
            stats = celery_app.control.inspect(timeout=1.0).stats() or {}
        except Exception:
            stats = {}
        state.should_stop = 0
        thread.join(timeout=15)
        for cfg in stats.values():
            for pid in (cfg.get("pool") or {}).get("processes", []):
                if isinstance(pid, int):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, 9)
        state.should_stop = None
        # Hosting a worker in-process arms Celery's "never call get() in a task"
        # guard process-wide; disarm it so later tests can collect results.
        _state._set_task_join_will_block(False)


def test_pool_child_sees_ops_only_after_turnover_confirms(live_worker: Any) -> None:
    from tai42_backend_celery.core import prefork
    from tai42_backend_celery.core.app import celery_app

    probe = live_worker

    # Seed the parent registry and re-fork so the live child is forked with it
    # (the boot child was forked before the registry was populated).
    probe.SERVED_TOOLS.clear()
    probe.SERVED_TOOLS.update({"alpha", "beta"})
    asyncio.run(prefork._on_fleet_op_applied("reload_config"))

    child_pid, tools = _submit(celery_app)
    assert tools == ["alpha", "beta"]

    # --- deregister_mcp: the parent registry loses "beta" ---
    probe.SERVED_TOOLS.discard("beta")

    # Without turnover the live child still serves the stale registry — a
    # main-process-only check would already read the new state.
    stale_pid, stale_tools = _submit(celery_app)
    assert stale_pid == child_pid
    assert stale_tools == ["alpha", "beta"]

    # A query op must NOT re-fork the pool (a read never re-forks).
    asyncio.run(prefork._on_fleet_op_applied("list_failed_mcps"))
    same_pid, _ = _submit(celery_app)
    assert same_pid == child_pid

    # The successor: re-fork the pool and CONFIRM turnover (real pool_restart +
    # stats poll).
    asyncio.run(prefork._on_fleet_op_applied("deregister_mcp"))
    dereg_pid, dereg_tools = _submit(celery_app)
    assert dereg_pid != child_pid  # a genuinely new child answered
    assert dereg_tools == ["alpha"]  # and it serves the post-deregister registry

    # --- reload_config: the parent registry gains "gamma" ---
    probe.SERVED_TOOLS.add("gamma")
    asyncio.run(prefork._on_fleet_op_applied("reload_config"))
    reload_pid, reload_tools = _submit(celery_app)
    assert reload_pid != dereg_pid
    assert reload_tools == ["alpha", "gamma"]
