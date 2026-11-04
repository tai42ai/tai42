"""The engine loop a fork-gate wait reports progress for: the one thread whose wait holds up the loop's next pass.

A backend's engine loop reports each of its passes to the process's liveness probe
(``BackendRuntime.report_progress``). A wait on the fork gate (a job span's entry, a guarded
fork) blocks the thread it runs on. When that thread runs the engine loop, the wait is what
holds up the loop's next pass: the loop is waiting out a re-import, not stalled, so the wait
reports progress for it once per ``tick_every`` seconds. On any other thread the loop passes
or stalls without that wait, and a report from it would claim passes the loop never made:
such a wait reports nothing.

A backend whose loop can wait on the gate runs the loop inside
:meth:`~tai42_kit.fork_gate.ForkGate.engine_loop`, on the loop's own thread. A backend whose
loop never waits on it runs none: a loop on an event loop's thread is one, since a fork there
never waits and a job span it enters is a
:meth:`~tai42_kit.fork_gate.ForkGate.job_span_async`, which waits off the loop.

A thread is ``threading.get_ident()``, so under a gevent-patched process each greenlet is a
thread of its own here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class LoopTick:
    """The engine loop's progress report: its thread, its tick, and the seconds between two ticks of a wait."""

    thread: int
    tick: Callable[[], None]
    tick_every: float


class EngineLoop:
    """The engine loop of one process, while one runs: the thread it runs on and the tick its gate waits report."""

    def __init__(self) -> None:
        """Start with no engine loop running."""
        self._lock = threading.Lock()
        self._running: LoopTick | None = None

    @contextmanager
    def running(self, *, tick: Callable[[], None], tick_every: float) -> Iterator[None]:
        """Run the calling thread as the engine loop for the block, its gate waits reporting ``tick``.

        One engine loop at a time: a block entered while another is open raises
        ``RuntimeError`` (a process has one engine loop to report for).
        """
        this = LoopTick(thread=threading.get_ident(), tick=tick, tick_every=tick_every)
        with self._lock:
            if self._running is not None:
                raise RuntimeError(
                    f"an engine loop already runs on thread {self._running.thread}; refusing a second one on "
                    f"thread {this.thread}"
                )
            self._running = this
        try:
            yield
        finally:
            with self._lock:
                if self._running is this:
                    self._running = None

    def tick_here(self) -> LoopTick | None:
        """The engine loop's report when the calling thread runs the engine loop, ``None`` on any other thread."""
        with self._lock:
            running = self._running
        if running is None or running.thread != threading.get_ident():
            return None
        return running

    def forget(self) -> None:
        """Drop the engine loop a forked child inherited: the child runs none of its parent's loops.

        FORK-CHILD ONLY. The lock is replaced too, as the fork may land while another thread
        holds it.
        """
        self._lock = threading.Lock()
        self._running = None
