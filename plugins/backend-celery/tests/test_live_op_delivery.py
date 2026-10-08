"""Live verification: a real prefork worker's forked child re-inherits the
parent's mutated tool registry only after the pool turnover confirms.

Runs a genuine Celery prefork worker (real ``fork`` / ``pool_restart`` / ``stats``
turnover) against a Redis broker, applies a mutating op in the parent, drives the
post-apply handler the shared backend base registers for this backend (op
filtering, manifest refresh and the Celery turnover, end to end), and asserts
through a pool child (``probe_registry`` returns the child pid + the registry it
was forked with).

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run it; once set, an
unreachable broker fails the module instead of skipping it. Unset, it probes
``redis://localhost:6399/0`` and skips when nothing answers there.
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

_BROKER_ENV = "TAI_TEST_CELERY_BROKER"
_BROKER_REQUIRED = _BROKER_ENV in os.environ
_BROKER = os.environ.get(_BROKER_ENV, "redis://localhost:6399/0")


def _redis_reachable(url: str) -> bool:
    """Whether a Redis broker answers at ``url``; a configured broker that does not answer raises."""
    import redis

    try:
        redis.Redis.from_url(url).ping()
    except redis.ConnectionError:
        if _BROKER_REQUIRED:
            raise
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


def _bus_op_applied(stub_app: Any) -> Any:
    """The post-apply handler the shared base wires for this backend — the real
    seam a bus op reaches, so the live path exercised here is the shipped one."""
    from tai42_backend_celery.core.backend import CeleryBackend, CeleryWorkerRuntime

    stub_app.lifecycle.fleet_op_applied.clear()
    CeleryBackend()._install_turnover(CeleryWorkerRuntime.from_args([]))
    return stub_app.lifecycle.fleet_op_applied[-1]


@contextlib.contextmanager
def _fresh_connection(celery_app: Any) -> Iterator[Any]:
    """A dedicated broker connection for one control call from the test driver.

    The driver shares its process with the worker, so every pool child is forked
    from it. A control call over the app's shared pool returns its connection with
    a reply ``BRPOP`` still in flight, and kombu's after-fork pool cleanup in each
    child then blocks reading that reply from the socket it shares with this
    process — the child never reports up and the pool re-forks it forever. A
    connection outside the pool is closed here and never reaches a child.
    """
    with celery_app.connection_for_write() as conn:
        yield conn


def _wait_worker_ready(prefork: Any, celery_app: Any, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if prefork._worker_ready.is_set() and prefork._local_nodename is not None:
            with _fresh_connection(celery_app) as conn:
                pong = celery_app.control.ping(timeout=1.0, connection=conn)
            if pong:
                return
        time.sleep(0.2)
    raise RuntimeError("live celery worker did not become ready in time")


@pytest.fixture
def live_worker() -> Iterator[Any]:
    """Boot a real prefork worker (concurrency 1) in a background thread, wired to
    the Redis broker, with the prefork-turnover signals connected. Torn down by
    requesting worker shutdown and reaping any surviving pool child."""
    from tai42_backend_celery.core import prefork
    from tai42_backend_celery.core.app import celery_app

    from . import _live_probe as probe  # registers the probe task on celery_app

    celery_app.conf.update(broker_url=_BROKER, result_backend=_BROKER)

    # Wire the turnover's Celery signals (node-name capture + readiness), exactly
    # as the worker runtime's ``build`` does.
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
        with _fresh_connection(celery_app) as conn:
            stats = celery_app.control.inspect(timeout=1.0, connection=conn).stats() or {}
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


def test_pool_child_sees_ops_only_after_turnover_confirms(live_worker: Any, stub_app: Any) -> None:
    from tai42_backend_celery.core.app import celery_app

    probe = live_worker
    bus_op_applied = _bus_op_applied(stub_app)

    # Seed the parent registry and re-fork so the live child is forked with it
    # (the boot child was forked before the registry was populated).
    probe.SERVED_TOOLS.clear()
    probe.SERVED_TOOLS.update({"alpha", "beta"})
    asyncio.run(bus_op_applied("reload_config", 30.0))

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
    asyncio.run(bus_op_applied("list_failed_mcps", 30.0))
    same_pid, _ = _submit(celery_app)
    assert same_pid == child_pid

    # The mutating op: re-fork the pool and CONFIRM turnover (real pool_restart +
    # stats poll).
    asyncio.run(bus_op_applied("deregister_mcp", 30.0))
    dereg_pid, dereg_tools = _submit(celery_app)
    assert dereg_pid != child_pid  # a genuinely new child answered
    assert dereg_tools == ["alpha"]  # and it serves the post-deregister registry

    # --- reload_config: the parent registry gains "gamma" ---
    probe.SERVED_TOOLS.add("gamma")
    asyncio.run(bus_op_applied("reload_config", 30.0))
    reload_pid, reload_tools = _submit(celery_app)
    assert reload_pid != dereg_pid
    assert reload_tools == ["alpha", "gamma"]
