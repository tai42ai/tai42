"""Prefork-pool turnover for this Celery worker process.

A Celery prefork worker forks children that inherit the tool registry at fork
time, so a bus op that mutates this process's registry has not truly applied
until the pool re-forks. :func:`turnover_local_pool` re-forks the local pool and
confirms it: it polls ``stats`` until every pre-restart child pid is gone and the
pool is back to full size, and raises otherwise (a bare ``pool_restart`` returns
once the restart is only armed). A raise turns this worker's bus reply into
``failed``.

The restart propagation itself is bounded: ``pool_restart`` can wedge inside
billiard on the pool parent's ``_putlock``, so it runs under a watchdog and, past
its share of the budget, the pre-restart children are killed outright so the pool
re-forks them. The re-fork is always the mechanism — the children are never
reloaded in place — and the same confirmation decides the verdict either way.

Only the Celery-shaped half lives here. WHICH bus ops warrant a turnover, the
``budget`` the whole turnover must land inside (sized under the bus apply window),
and the manifest env the re-forked children inherit are all host-shaped, so the
shared backend base owns them and hands the budget in.
:func:`register` is worker-scoped — called from the worker runtime's ``build``,
never at import — and the turnover no-ops on a non-prefork pool.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from celery import signals

from tai42_backend_celery.core.app import celery_app

logger = logging.getLogger(__name__)

# Per-control-call timeout and the pause between turnover polls; the whole-turnover
# budget is the caller's, passed into :func:`turnover_local_pool`.
_POOL_CONTROL_TIMEOUT = 2.0
_POOL_RESTART_TIMEOUT = 10.0
_POOL_RECYCLE_POLL_INTERVAL = 0.5

# The share of the turnover budget still unspent when the restart is issued that
# the propagation may consume before the kill guard fires. The remainder pays for
# the kill and the confirmation, so the whole turnover — wedge included — still
# lands inside the caller's apply window and reports ``failed`` rather than
# letting the bus ack time out.
_RESTART_PROPAGATION_SHARE = 0.5

# This worker's own node name (captured at setup) and a flag set once the worker
# is consuming control commands. The handler addresses ``pool_restart`` / ``stats``
# to this name.
_local_nodename: str | None = None
_worker_ready = threading.Event()


def register() -> None:
    """Prepare this worker process for pool turnover (worker-scoped, called from
    the worker runtime's ``build``, never at import): connect the setup/ready
    signals that tell :func:`turnover_local_pool` who to address and when.

    The post-apply hook itself is not wired here — the shared backend base owns
    which ops warrant a turnover and drives this module through the runtime.
    """
    signals.celeryd_after_setup.connect(_record_local_nodename)
    signals.worker_ready.connect(_mark_worker_ready)


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


def turnover_local_pool(reason: str, budget: float) -> None:
    """Restart the local prefork pool within ``budget`` seconds and confirm the
    whole pool re-forked. Blocking (control I/O), so the caller runs it off the
    serving loop; a raise propagates, failing the op named by ``reason``.

    ``budget`` is the caller's: the whole turnover — the wait for a consuming
    worker, the restart, the kill guard and the confirmation — is spent inside it,
    so a stall reports a truthful ``failed`` before the caller's own window closes.

    No-op for a non-prefork pool and before the worker reaches setup
    (``_local_nodename`` is ``None`` — its next-forked children inherit the post-op
    registry). A worker that reached setup but is not yet consuming control
    commands may have a bootstrap child forked from the pre-op registry it cannot
    yet recycle, so the turnover WAITS (bounded by the budget) for it to start
    consuming, then recycles; a worker that never starts consuming raises loudly."""
    if _local_nodename is None:
        logger.debug("prefork turnover skipped for %s: worker has not reached setup (no forked pool yet)", reason)
        return
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
    _restart_pool_and_confirm(hostname, before, max(0.0, deadline - time.monotonic()))


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


def _restart_pool_and_confirm(hostname: str, before: tuple[set[int], int], budget: float) -> None:
    """Re-fork the pool within ``budget``: arm the restart under a bound, kill the
    pre-restart children if that wedges, and confirm the turnover either way.

    The restart carries no hard bound of its own — the control call bounds only the
    reply drain, not the publish nor the pool-side apply, and the apply can wedge
    indefinitely inside billiard on the pool parent's ``_putlock``. So the arm gets
    a share of the budget and the rest pays for the hard path, which is the same
    re-fork by a harsher route. :func:`_confirm_turnover` is the verdict in both
    cases, and it raises when the pool did not turn over.
    """
    deadline = time.monotonic() + budget
    propagation_timeout = _restart_propagation_timeout(budget)
    wedged = not _await_restart(hostname, propagation_timeout)
    if wedged:
        logger.error(
            "[%s] pool_restart did not return within %.2fs — wedged inside billiard (pool parent's _putlock); "
            "killing pre-restart pool children %s so the pool re-forks",
            hostname,
            propagation_timeout,
            sorted(before[0]),
        )
        _kill_pool_children(hostname, before[0])
    try:
        _confirm_turnover(hostname, before, max(0.0, deadline - time.monotonic()))
    except RuntimeError as exc:
        if wedged:
            raise RuntimeError(
                f"[{hostname}] pool_restart wedged inside billiard (_putlock) and killing its children did not "
                f"bring the pool back either: {exc}"
            ) from exc
        raise


def _restart_propagation_timeout(budget: float) -> float:
    """The bound on the restart propagation: a share of the turnover budget left
    when the restart is issued, so the kill guard fires with the rest of the budget
    still available for the kill and the confirmation."""
    return max(0.0, budget) * _RESTART_PROPAGATION_SHARE


def _await_restart(hostname: str, timeout: float) -> bool:
    """Run :func:`_restart_pool` on a watchdog thread, waiting at most ``timeout``.

    Returns ``False`` when the call has not returned by the bound. It then stays
    wedged on its own daemon thread — a Python thread cannot be cancelled — and a
    reply landing later only re-arms an already-completed turnover. A failure
    raised by the restart itself surfaces here, in the caller's thread.
    """
    failures: list[Exception] = []
    done = threading.Event()

    def _arm() -> None:
        # Captured, never raised out of the thread: the caller re-raises it, so the
        # op fails on the calling stack instead of dying in a thread hook.
        try:
            _restart_pool(hostname)
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()

    threading.Thread(target=_arm, name=f"tai42-pool-restart[{hostname}]", daemon=True).start()
    if not done.wait(timeout):
        return False
    if failures:
        raise failures[0]
    return True


def _kill_pool_children(hostname: str, pids: set[int]) -> None:
    """SIGKILL this worker's pre-restart pool children so billiard reaps and
    re-forks them (never an in-place reload: only a fork re-inherits the registry).

    The hard path is deliberately OS-level. Celery's own child-level hard stop
    (``billiard.pool.Pool.terminate_job``) is itself an ``os.kill`` on the child
    pid, but reaching it needs the in-process pool handle, and every route to it
    from this seam — another ``pool_restart``, ``pool_shrink``, a ``revoke`` with
    ``terminate`` — queues behind the very control/pool machinery that is wedged.
    Reaping a killed child is also what releases the parent's ``_putlock``
    (``Pool._maintain_pool``), so the kill unwedges the pool it re-forks. SIGKILL,
    not SIGTERM: a child wedged in the pool's queue machinery need never reach the
    point where its termination handler runs, and this path runs only past the
    bound. The pids are this worker's own children, read from its ``stats`` moments
    earlier; a signal failure other than an already-reaped child propagates.
    """
    parent = os.getpid()
    for pid in sorted(pids):
        if pid <= 0 or pid == parent:
            # Never signal the worker parent: that takes the node down instead of
            # recycling its pool.
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            logger.warning("[%s] pool child %s was already gone when the kill guard fired", hostname, pid)
        else:
            logger.error("[%s] killed wedged pool child %s (SIGKILL); billiard re-forks it", hostname, pid)


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
