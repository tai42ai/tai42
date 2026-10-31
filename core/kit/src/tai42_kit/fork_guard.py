"""The fork guard: a job span of the fork gate held around every fork of the process.

A forking consumer installs it once (:meth:`~tai42_kit.fork_gate.ForkGate.guard_forks`),
and from then on every fork of the process waits out a running re-import, whichever library
forks. The guard works through ``os.register_at_fork``:

* the ``before`` hook enters a job span on the forking thread, waiting as
  :meth:`~tai42_kit.fork_gate.ForkGate.job_span` waits with the guard's ``timeout``,
  ``tick`` and ``tick_every``, so a fork waits out a running re-import and reports while it
  waits; a fork in a context that carries an open job span nests in it and enters none;
  a fork on a thread that runs an event loop never waits (below);
* ``after_in_parent`` leaves that span (CPython runs the parent hooks on a failed fork too);
* ``after_in_child`` resets the child's copy of the gate
  (:meth:`~tai42_kit.fork_gate.ForkGate.reset_after_fork`): a child inherits whatever holds
  were live at the fork instant, and only the parent can ever release them.

Every fork that runs the interpreter's fork hooks is covered (``os.fork``,
``multiprocessing``'s fork context and the libraries built on it, and a ``subprocess``
started with a ``preexec_fn``, whose child runs Python before it execs). A ``subprocess``
started without one forks without the hooks and runs no Python before it execs.

A fork made by a rebuild's own body (in the context that body runs in, on whichever thread,
or in a :meth:`~tai42_kit.fork_gate.ForkGate.fresh_context` or a
:meth:`~tai42_kit.fork_gate.ForkGate.rebuild_context` taken from it) enters no span: it
is the rebuild's own fork, not a job racing it, and a span could only wait for the rebuild to
end. Once that rebuild ends, the same context forks like any other. A step the body runs in a
bare ``contextvars.Context()``, or on a thread started without its context, carries no mark,
and its fork waits on the rebuild like a job's.

That pass is not a protection: the gate does not know which thread is inside an import. A
fork the body makes on one thread while the body imports on another thread copies that
import's held module lock into the child, as an unguarded fork would. A rebuild body that
forks keeps its forks on the thread that imports, or out of its imports' span.

A fork on a thread that runs an event loop (a coroutine, or sync code it calls, that forks)
must never block that loop: a rebuild's body may run on it (a coroutine rebuild, or a
rebuild that hands steps to the loop), and so may the jobs whose spans the rebuild waits
out. Such a fork enters its span at once. A rebuild still waiting for job spans to close
waits out the fork instant; a fork that lands while a rebuild already holds the gate forks
inside the re-import, which the hook cannot prevent, and reports it at ERROR. Code that forks
from a loop holds :meth:`~tai42_kit.fork_gate.ForkGate.job_span_async` around the fork
instead, which waits without blocking the loop.

The hook is the net for forks the process does not make itself. CPython reports an exception
raised in a ``before`` hook and forks anyway, so a stop request raised while the hook waits on
a re-import cannot stop the fork. Code that forks on its own call holds
:meth:`~tai42_kit.fork_gate.ForkGate.job_span` around that call instead: there the exception
propagates and nothing is forked, and the hook's fork nests in that span.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_kit.fork_gate import ForkGate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForkGuard:
    """The job span the fork guard holds around each fork of the process: its wait and its progress report."""

    timeout: float
    tick: Callable[[], None]
    tick_every: float


class ForkHooks:
    """The fork hooks of one gate: the guard they hold, and what they took for each fork in progress.

    ``os.register_at_fork`` cannot be undone, so the hooks are registered once per gate and
    read the guard at fork time; a gate whose guard is dropped (``guard = None``) forks
    unguarded. Every field is read and written under the gate's lock.
    """

    def __init__(self, gate: ForkGate) -> None:
        """Create the hooks of ``gate``, with no guard installed and nothing registered."""
        self._gate = gate
        self.guard: ForkGuard | None = None
        self._registered = False
        # Per forking thread, what the ``before`` hook took for each fork in progress: the
        # span it registered with the gate generation it registered under, or ``None`` for a
        # fork it let through. The parent hook closes the latest one.
        self.fork_spans: dict[int, list[tuple[object, int] | None]] = {}

    def install(self, guard: ForkGuard) -> None:
        """Hold ``guard``'s span around every fork from now on.

        One guard per gate: installing the same guard again changes nothing, another one
        raises ``RuntimeError`` (a process has one work loop to report for).
        """
        with self._gate._cond:
            if self.guard is not None:
                if self.guard == guard:
                    return
                raise RuntimeError(f"the fork gate already guards forks with {self.guard}; refusing {guard}")
            self.guard = guard
            register = not self._registered
            self._registered = True
        if register:
            os.register_at_fork(
                before=self.before, after_in_parent=self.after_in_parent, after_in_child=self.after_in_child
            )

    def forget_forks(self) -> None:
        """Drop what the hooks took for the forks in progress (a child's reset gate owns none)."""
        self.fork_spans = {}

    def before(self) -> None:
        """Open the guard's job span on the forking thread (the ``before`` fork hook).

        A fork in a rebuild's own context, or inside a job span open in this context, is let
        through. Otherwise the span is recorded and registered in one locked step of the
        gate's span entry, so an exception raised in this hook (which CPython reports and
        then forks anyway) never leaves a span open that the parent hook cannot close.

        A fork on a thread that runs an event loop enters its span without waiting (see the
        module docstring); one that lands while a rebuild holds the gate is reported at ERROR.
        """
        guard = self.guard
        if guard is None:
            return
        tid = threading.get_ident()
        with self._gate._cond:
            if self._gate._runs_own_work():
                self.fork_spans.setdefault(tid, []).append(None)
                return
        span = object()

        def record(generation: int) -> None:
            self.fork_spans.setdefault(tid, []).append((span, generation))

        if not _runs_an_event_loop():
            self._gate._enter_reported_span(span, guard.timeout, guard.tick, guard.tick_every, record)
        elif self._gate._enter_span_at_once(span, record):
            logger.error(
                "fork gate: a fork on an event loop's thread while a module re-import holds the gate "
                "cannot wait for it without blocking the loop the re-import may need; forking anyway "
                "— the child may deadlock on an inherited import lock"
            )

    def after_in_parent(self) -> None:
        """Close the span the ``before`` hook opened on this thread (the parent's fork hook)."""
        tid = threading.get_ident()
        with self._gate._cond:
            taken = self.fork_spans.get(tid)
            if not taken:
                return
            span = taken.pop()
            if not taken:
                del self.fork_spans[tid]
        if span is not None:
            self._gate._leave_span(*span)

    def after_in_child(self) -> None:
        """Reset the child's copy of the gate (the child's fork hook)."""
        if self.guard is not None:
            self._gate.reset_after_fork()


def _runs_an_event_loop() -> bool:
    """Whether the calling thread is running an event loop (the fork is made from code on that loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
