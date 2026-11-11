"""What runs a process's compute jobs, by the kind of process: the first of these that applies.

- A process forked, at any remove, from one that shares its pool (:func:`share_compute_pool`) or
  from one whose pool host had started and was not stopped by it: that pool or host, reached
  over the end it kept at its fork. It never stops or replaces it.
- A process started through a process prelude, or forked at any remove from one (a fork inherits
  the variable that names the prelude): a pool host of its own, which its first job starts
  (:mod:`~tai42_kit.compute.pool_host`).
- Any other process: its own :class:`~tai42_kit.compute.pool.ComputePool`. A daemonic one has
  nowhere to run a job: the standard library refuses a daemonic process children, and its job
  raises that refusal.

A process's own pool is started (sized by ``COMPUTE_WORKERS``) on its first ``compute_pool()`` call,
or on the first job of a child it shares the pool with; a pool host, which runs a pool of its own,
on its first compute job. A child that reaches another process's pool or host starts no workers, so
it may be short-lived or daemonic. In any forked child the pool state it inherited is reset, and the
list above picks again. A broken pool (an idle worker died) is stopped, its pending jobs raising
:class:`~tai42_kit.compute.errors.ComputePoolUnavailableError`, and replaced by the next such call
or job, with a warning; a pool host whose closed end refused one of its owner's jobs is stopped
and replaced, with a warning, and that job is sent to the fresh one. A host a process reaches and
did not start is never replaced: once it ended, that process's jobs raise
:class:`~tai42_kit.compute.errors.ComputePoolUnavailableError`. The lifespan that owns the process
stops its pool, or its pool host, with :func:`stop_compute_pool`; one still running when its process
exits is stopped by that exit; a worker whose pool's process was killed ends once the job it runs
returns and it reads its closed pipe; a pool host whose owner was killed ends when it reads its
closed socket.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing.process
import os
import threading

from tai42_kit.compute.fork_share import PoolShare
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.pool_host import HostedPool, PoolHost, serve_pool
from tai42_kit.compute.pool_wire import ComputePoolClient
from tai42_kit.compute.settings import compute_settings
from tai42_kit.compute.wire_sockets import wire_sockets

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: ComputePool | None = None
_share: PoolShare | None = None
# The pool this process reaches and did not start: the one the process it was forked from shares, runs a
# host for, or reaches.
_reached: ComputePoolClient | None = None
# The pool host this process started.
_host: PoolHost | None = None


def compute_pool() -> ComputePool | ComputePoolClient | HostedPool:
    """Return what runs this process's compute jobs, by the kind of process the module's list names.

    That is the client of the pool or host the process reaches, its
    :class:`~tai42_kit.compute.pool_host.HostedPool`, or its own
    :class:`~tai42_kit.compute.pool.ComputePool`, which the first call starts.
    """
    reached = _reached
    if reached is not None:
        return reached
    if not ComputePool.runs_in_this_process():
        return _hosted
    return _own_pool()


def _own_pool() -> ComputePool:
    global _pool
    with _lock:
        pool = _pool
        if pool is not None and pool.broken:
            _pool = None
            pool.stop()
            pool = _pool = _started_pool()
            logger.warning("compute pool replaced after its workers died")
        elif pool is None:
            pool = _pool = _started_pool()
        return pool


def _started_pool() -> ComputePool:
    pool = ComputePool(compute_settings().workers)
    pool.start()
    return pool


def _hosted_client() -> ComputePoolClient:
    """The client of the pool host this process's jobs go to: the one it reaches, else its current host's."""
    reached = _reached
    if reached is not None:
        return reached
    return _current_host().client


def _current_host() -> PoolHost:
    global _host
    with _lock:
        host = _host
        if host is not None and host.client.server_ended:
            _host = None
            host.stop()
            host = _host = _started_host()
            logger.warning("compute pool host replaced after it ended")
        elif host is None:
            host = _host = _started_host()
        return host


# The jobs of a process started through a process prelude run through it.
_hosted = HostedPool(_hosted_client)


def _started_host() -> PoolHost:
    host = PoolHost(_serve_as_host)
    # Registered again on every start: exit functions run last registered first, so the host is
    # stopped before every exit function registered before its start.
    atexit.unregister(_stop_host)
    atexit.register(_stop_host)
    return host


def _serve_as_host(fd: int) -> None:
    """A pool host's serving function: serve the end ``fd`` with the host's own pool."""
    serve_pool(fd, _own_pool, stop_compute_pool)


def share_compute_pool() -> None:
    """Let every child this process forks from now on run its compute jobs in this process's pool.

    Starts the serving thread, not the pool: the pool starts on the first job, the process's own
    or a child's. A second call changes nothing. In a process that itself reaches another
    process's pool it changes nothing either: that process's children reach the same pool; nor in
    a process started through a process prelude or forked at any remove from one, whose
    children's jobs run where the module's list says. Raises
    :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError` when the share cannot start
    serving (a process at its descriptor limit, or a serving thread that cannot start or run a
    loop of its own); nothing is shared then.
    """
    global _share
    with _lock:
        if _share is None and _reached is None and ComputePool.runs_in_this_process():
            _share = PoolShare(_own_pool)


def stop_compute_pool() -> None:
    """Stop this process's own compute pool and the pool host it started; nothing to do when none runs.

    A process that reaches another process's pool has neither. Sharing outlives the stop: a
    child's next job starts a fresh pool here; and a later job of this process starts a fresh pool
    or host.
    """
    global _pool, _host
    with _lock:
        pool, _pool = _pool, None
        host, _host = _host, None
    if pool is not None:
        pool.stop()
    if host is not None:
        host.stop()


def _stop_host() -> None:
    global _host
    with _lock:
        host, _host = _host, None
    if host is not None:
        host.stop()


def _settle_in_forked_child() -> None:
    # The child must neither use nor stop the parent's pool or host: stopping either would end the
    # parent's workers. A lock held by a parent thread at the fork instant is never released here.
    # A child has no children: the parent's workers stay in the child's copy of the stdlib's
    # registry, where a child's exit cleanup would terminate or join them. The child reaches the
    # pool its parent's children send their jobs to: the parent's own over its share, the host the
    # parent runs, or the pool the parent reaches; the wire's fork hooks let go of the other ends,
    # as ``wire_sockets`` states.
    global _lock, _pool, _share, _reached, _host
    _lock = threading.Lock()
    _pool = None
    multiprocessing.process._children.clear()  # pyright: ignore[reportAttributeAccessIssue]
    _share = None
    _host = None
    kept = wire_sockets.kept_in_children
    _reached = None if kept is None else ComputePoolClient(kept)


os.register_at_fork(after_in_child=_settle_in_forked_child)
