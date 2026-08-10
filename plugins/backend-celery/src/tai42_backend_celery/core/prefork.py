"""Prefork-pool turnover after a worker-bus op applies in this process.

A Celery prefork worker forks children that inherit the tool registry at fork
time, so a bus op that mutates this process's registry has not truly applied
until the pool re-forks. The ``on_fleet_op_applied`` handler re-forks the local
pool and confirms it: it polls ``stats`` until every pre-restart child pid is
gone and the pool is back to full size, and raises otherwise (a bare
``pool_restart`` returns once the restart is only armed). A raise turns this
worker's bus reply into ``failed``.

The turnover runs inside the op apply, before the terminal reply, so its budget
is derived from the bus apply window (``TAI_BUS_APPLY_TIMEOUT``) and sits a margin
under it, letting a stall report a truthful ``failed`` rather than a guessed
``timed_out``. Registration is worker-scoped (called from ``launch``, never at
import) and no-ops on a non-prefork pool and on non-mutating ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from celery import signals
from tai42_contract.app import tai42_app

from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.settings import celery_settings

logger = logging.getLogger(__name__)

# The bus ops that mutate this process's tool registry, so the forked children
# must be recycled to re-inherit it (the query op ``list_failed_mcps`` is absent).
_MUTATING_OPS: frozenset[str] = frozenset(
    {
        "reload_config",
        "reload_mcp",
        "deregister_mcp",
        "reload_tool",
        "remove_tool",
        "reload_failed_mcps",
    }
)

# Per-control-call timeout and the pause between turnover polls; the whole-turnover
# budget is derived per call (see :func:`_turnover_budget`).
_POOL_CONTROL_TIMEOUT = 2.0
_POOL_RESTART_TIMEOUT = 10.0
_POOL_RECYCLE_POLL_INTERVAL = 0.5

# The whole-turnover budget is derived from ``TAI_BUS_APPLY_TIMEOUT`` (the bus
# apply window) less a margin, and floored, so the confirm-or-raise reaches the
# publisher before its report cut and a stall records a truthful ``failed``.
_BUS_APPLY_TIMEOUT_ENV = "TAI_BUS_APPLY_TIMEOUT"
_BUS_APPLY_TIMEOUT_DEFAULT = 30.0
_TURNOVER_BUDGET_MARGIN = 3.0
_TURNOVER_BUDGET_FLOOR = 5.0

# This worker's own node name (captured at setup) and a flag set once the worker
# is consuming control commands. The handler addresses ``pool_restart`` / ``stats``
# to this name.
_local_nodename: str | None = None
_worker_ready = threading.Event()


def register() -> None:
    """Wire the prefork-pool turnover into this worker process (worker-scoped,
    called from ``launch``, never at import): connect the setup/ready signals and
    register the post-apply handler on the app's lifecycle seam."""
    signals.celeryd_after_setup.connect(_record_local_nodename)
    signals.worker_ready.connect(_mark_worker_ready)
    tai42_app.lifecycle.on_fleet_op_applied(_on_fleet_op_applied)


def _record_local_nodename(sender: Any = None, instance: Any = None, **kwargs: Any) -> None:
    """Capture this worker's node name from the ``celeryd_after_setup`` signal."""
    global _local_nodename
    name = getattr(instance, "hostname", None) or sender
    if name:
        _local_nodename = str(name)


def _mark_worker_ready(sender: Any = None, **kwargs: Any) -> None:
    """Mark the worker ready to answer control commands, so the turnover only runs
    once the local pidbox is consuming."""
    _worker_ready.set()


async def _on_fleet_op_applied(op_name: str) -> None:
    """Re-fork and confirm the local prefork pool after a mutating bus op.

    Non-mutating ops return early. The turnover's control I/O is blocking, so it
    runs off the serving loop; a raise propagates, failing the op's terminal reply.
    """
    if op_name not in _MUTATING_OPS:
        return
    await asyncio.to_thread(_turnover_local_pool, op_name)


def _turnover_local_pool(op_name: str) -> None:
    """Restart the local prefork pool and confirm the whole pool re-forked.

    No-op for a non-prefork pool and before the worker reaches setup
    (``_local_nodename`` is ``None`` — its next-forked children inherit the post-op
    registry). A worker that reached setup but is not yet consuming control
    commands may have a bootstrap child forked from the pre-op registry it cannot
    yet recycle, so the turnover WAITS (bounded by the budget) for it to start
    consuming, then recycles; a worker that never starts consuming raises loudly."""
    if _local_nodename is None:
        logger.debug("prefork turnover skipped for %s: worker has not reached setup (no forked pool yet)", op_name)
        return
    budget = _turnover_budget()
    deadline = time.monotonic() + budget
    if not _worker_ready.is_set() and not _worker_ready.wait(timeout=budget):
        raise RuntimeError(
            f"[{_local_nodename}] worker did not start consuming control commands within {budget}s; "
            "cannot recycle a possibly-stale prefork pool after a mutating bus op"
        )
    hostname = _local_nodename
    before = _prefork_pool_state(hostname)
    if before is None:
        return
    _refresh_manifest_env()
    _restart_pool(hostname)
    _confirm_turnover(hostname, before, max(0.0, deadline - time.monotonic()))


def _turnover_budget() -> float:
    """The whole-turnover budget: ``TAI_BUS_APPLY_TIMEOUT`` less a margin, floored,
    so the confirm-or-raise lands before the publisher's report cut. A malformed
    value raises."""
    raw = os.environ.get(_BUS_APPLY_TIMEOUT_ENV)
    apply_timeout = _BUS_APPLY_TIMEOUT_DEFAULT if raw is None else float(raw)
    return max(_TURNOVER_BUDGET_FLOOR, apply_timeout - _TURNOVER_BUDGET_MARGIN)


@contextmanager
def _control_connection() -> Iterator[Any]:
    """A fresh, short-lived broker connection for one turnover control call, never
    the app's shared pool: the pooled connection is inherited by the prefork
    children, and ``pool_restart`` terminating them would break the parent's next
    write over it."""
    with celery_app.connection_for_write() as conn:
        yield conn


def _pool_pids(pool: dict[str, Any]) -> set[int]:
    """The child PIDs a prefork worker reports in its ``stats`` pool section."""
    return {p for p in pool.get("processes", []) if isinstance(p, int)}


def _prefork_pool_state(hostname: str) -> tuple[set[int], int] | None:
    """This worker's prefork pool state ``(child_pids, max_concurrency)`` from a
    single ``stats`` call, or ``None`` when the pool is not prefork (no forked
    children to recycle). A worker that does not answer ``stats``, or answers
    without a pool section, raises: its turnover cannot be verified.
    """
    with _control_connection() as conn:
        stats = (
            celery_app.control.inspect(
                destination=[hostname], timeout=_POOL_CONTROL_TIMEOUT, limit=1, connection=conn
            ).stats()
            or {}
        )
    cfg = stats.get(hostname)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"worker {hostname} did not answer stats; cannot verify its pool for turnover")
    pool = cfg.get("pool")
    if not isinstance(pool, dict):
        raise RuntimeError(f"worker {hostname} stats carried no pool section; cannot verify turnover")
    if "prefork" not in str(pool.get("implementation", "")).lower():
        return None
    pids = _pool_pids(pool)
    max_conc = pool.get("max-concurrency")
    return pids, max_conc if isinstance(max_conc, int) and max_conc > 0 else len(pids)


def _refresh_manifest_env() -> None:
    """Publish the live manifest JSON into the env so the re-forked children
    inherit the current registry."""
    os.environ[celery_settings().manifest_key] = json.dumps(tai42_app.admin.live_manifest, separators=(",", ":"))


def _restart_pool(hostname: str) -> None:
    """Arm this worker's pool restart; the turnover itself is confirmed by
    :func:`_confirm_turnover`."""
    with _control_connection() as conn:
        replies = celery_app.control.broadcast(
            "pool_restart",
            arguments={"reload": False},
            reply=True,
            destination=[hostname],
            timeout=_POOL_RESTART_TIMEOUT,
            # One addressed worker, one reply: return on it, not the full timeout.
            limit=1,
            connection=conn,
        )
    reply = next(iter(replies or []), {}).get(hostname, {})
    if not (isinstance(reply, dict) and "ok" in reply):
        raise RuntimeError(f"pool restart request failed for {hostname}: {reply!r}")


def _confirm_turnover(hostname: str, before: tuple[set[int], int], timeout: float) -> None:
    """Poll ``stats`` until the pool has fully re-forked (every pre-restart child
    pid gone AND the pool back to full size), or raise loudly at the deadline. This
    holds the bus reply until the re-fork completes, since ``pool_restart`` returns
    once merely armed — a fast follow-up read could still reach a pre-restart child.
    """
    old_pids, max_conc = before
    surviving = old_pids
    size = 0
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with _control_connection() as conn:
            stats = (
                celery_app.control.inspect(
                    destination=[hostname], timeout=min(_POOL_CONTROL_TIMEOUT, remaining), limit=1, connection=conn
                ).stats()
                or {}
            )
        cfg = stats.get(hostname)
        if isinstance(cfg, dict) and isinstance(cfg.get("pool"), dict):
            new_pids = _pool_pids(cfg["pool"])
            surviving = old_pids & new_pids
            size = len(new_pids)
            if not surviving and size >= max_conc:
                return
        nap = min(_POOL_RECYCLE_POLL_INTERVAL, max(0.0, deadline - time.monotonic()))
        if nap == 0.0:
            break
        time.sleep(nap)
    if surviving:
        detail = f"old children still present: {sorted(surviving)}; they may still serve stale tools"
    else:
        detail = f"pool came back short: {size}/{max_conc} children"
    raise RuntimeError(f"[{hostname}] prefork pool did not fully recycle within {timeout}s ({detail})")
