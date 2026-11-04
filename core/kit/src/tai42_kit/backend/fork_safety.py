"""Fork safety of a forking backend process: the fork gate's guard, and a clean monitoring client in every child.

A backend whose process forks (a pool of child processes, a forked scheduler) installs
:func:`guard_forks` once, instead of the gate's own
:meth:`~tai42_kit.fork_gate.ForkGate.guard_forks`. Every fork then holds the gate's job span
(see :mod:`tai42_kit.fork_guard`), and every child evicts the monitoring writer it inherited:
the writer's parent-owned background threads do not survive ``fork()`` and would hang its
flushes in the child, which rebuilds a clean writer on first use.
"""

from __future__ import annotations

import logging
import os

from tai42_contract.app import tai42_app

from tai42_kit.fork_gate import ForkGate

logger = logging.getLogger(__name__)


def evict_inherited_monitoring() -> None:
    """Shut down the monitoring writer a forked child inherited; the child rebuilds it on first use."""
    logger.info("forked child %s: evicting the inherited monitoring client; it rebuilds on first use", os.getpid())
    tai42_app.monitoring.active.writer.shutdown()


def guard_forks(gate: ForkGate, *, timeout: float) -> None:
    """Guard every fork of this process with ``gate`` and evict the inherited monitoring writer in every child.

    ``timeout`` is the guard's job span wait, as :meth:`~tai42_kit.fork_gate.ForkGate.guard_forks`
    takes it; installing again with the same one changes nothing, another one raises
    ``RuntimeError``. A fork waiting on the engine loop's thread reports the loop's progress
    (:meth:`~tai42_kit.fork_gate.ForkGate.engine_loop`); one on any other thread reports nothing.
    """
    gate.guard_forks(timeout=timeout, after_in_child=evict_inherited_monitoring)
