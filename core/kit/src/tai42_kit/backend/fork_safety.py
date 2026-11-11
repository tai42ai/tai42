"""Fork safety of a forking backend process: the fork gate's guard, and a clean monitoring client in every child.

A backend whose process forks (a pool of child processes, a forked scheduler) installs
:func:`guard_forks` once, instead of the gate's own
:meth:`~tai42_kit.fork_gate.ForkGate.guard_forks`. Every fork then holds the gate's job span
(see :mod:`tai42_kit.fork_guard`), and every child evicts the monitoring writer it inherited:
the writer's parent-owned background threads do not survive ``fork()`` and would hang its
flushes in the child, which rebuilds a clean writer on first use.

In a process whose threads are OS threads, every child runs its compute jobs in this process's
compute pool, or in the pool this process itself reaches
(:func:`tai42_kit.compute.share_compute_pool`), while it keeps the descriptors it inherited, so
such a child may be daemonic or live for one job; a child that closes its inherited descriptors
gets :class:`~tai42_kit.compute.ComputePoolUnavailableError` on a job. A process started through
a process prelude (:data:`~tai42_contract.backend.PROCESS_PRELUDE_ENV` names one) shares nothing:
:mod:`tai42_kit.compute.process_pool` states what runs its children's jobs.
"""

from __future__ import annotations

import logging
import os

from tai42_contract.app import tai42_app

from tai42_kit.compute import share_compute_pool
from tai42_kit.fork_gate import ForkGate

logger = logging.getLogger(__name__)


def evict_inherited_monitoring() -> None:
    """Shut down the monitoring writer a forked child inherited; the child rebuilds it on first use."""
    logger.info("forked child %s: evicting the inherited monitoring client; it rebuilds on first use", os.getpid())
    tai42_app.monitoring.active.writer.shutdown()


def guard_forks(gate: ForkGate, *, timeout: float) -> None:
    """Guard every fork of this process with ``gate``, and set every forked child up to run.

    Each child has the inherited monitoring writer evicted, and runs its compute jobs where
    :func:`~tai42_kit.compute.share_compute_pool`, asked first, decides: in a process whose
    threads are OS threads, this process's pool or the pool this process itself reaches, while it
    keeps the descriptors it inherited; a process started through a process prelude shares nothing.

    ``timeout`` is the guard's job span wait, as :meth:`~tai42_kit.fork_gate.ForkGate.guard_forks`
    takes it; installing again with the same one changes nothing, another one raises
    ``RuntimeError``. Raises :class:`~tai42_kit.compute.ComputePoolUnavailableError`, with no
    guard installed, when the compute pool's share cannot start serving. A fork waiting on the
    engine loop's thread reports the loop's progress
    (:meth:`~tai42_kit.fork_gate.ForkGate.engine_loop`); one on any other thread reports nothing.
    """
    share_compute_pool()
    gate.guard_forks(timeout=timeout, after_in_child=evict_inherited_monitoring)
