"""The process's compute pool: one :class:`~tai42_kit.compute.pool.ComputePool` per process.

:func:`compute_pool` returns it, starting it (sized by ``COMPUTE_WORKERS``) on the first call
in the process, so every process that runs a compute job has one, whichever entry started the
process. A pool the library reports broken (an idle worker died) is stopped and replaced by the
next call, with a warning. A host whose lifespan owns the process stops it with
:func:`stop_compute_pool`; a later call starts a fresh one. A forked child does not use the pool
it inherited (its manager threads exist only in the parent and its workers belong to the parent)
and holds no record of the parent's workers as its children: the child's first call starts the
child's own pool. The pool library terminates a process's workers when the interpreter exits; a
worker whose owning process ended without that step ends when it next reads its closed task pipe.
"""

from __future__ import annotations

import logging
import multiprocessing.process
import os
import threading

from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.settings import compute_settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: ComputePool | None = None


def compute_pool() -> ComputePool:
    """Return this process's running compute pool, starting it on the first call in the process.

    A broken pool is stopped (its pending jobs raise
    :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError`) and a fresh one started.
    """
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


def stop_compute_pool() -> None:
    """Stop this process's compute pool; nothing to do when none is running."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.stop()


def _forget_inherited_pool() -> None:
    # The child must neither use nor stop the parent's pool: stopping it would terminate the
    # parent's workers. A lock held by a parent thread at the fork instant is never released here.
    # A child has no children: the parent's workers stay in the child's copy of the stdlib's
    # registry, where a child's exit cleanup would terminate or join them.
    global _lock, _pool
    _lock = threading.Lock()
    _pool = None
    multiprocessing.process._children.clear()  # pyright: ignore[reportAttributeAccessIssue]


os.register_at_fork(after_in_child=_forget_inherited_pool)
