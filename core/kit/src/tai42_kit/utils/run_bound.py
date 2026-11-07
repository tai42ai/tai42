"""The bound of a bounded run: the latest the run ends, and the work it owes at its exit.

A dispatched backend job runs under its job budget, which no backend can lengthen once the
job runs, and a live caller's turn runs under the turn budget (``TAI_TURN_TIMEOUT_SECONDS``)
once that budget is armed. Work whose length the run does not control — a fleet recycle roll,
which waits each target's announced drain — reads :func:`run_bound` and carries itself
somewhere no such bound cuts it. The bound tells such work when the run ends
(:meth:`RunBound.seconds_left`) and takes the work the run owes at its exit
(:meth:`RunBound.once_ended`), which :func:`bounded_run` runs after the run's body returned,
raised or was cancelled. Homed in kit so both the skeleton that reads it and the execution
backends below the skeleton (which never import tai42-skeleton) can set it.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

_run_bound: ContextVar["RunBound | None"] = ContextVar("tai42_run_bound", default=None)


class RunBound:
    """The latest a run ends, fixed when it started, and the work the run owes at its exit."""

    def __init__(self, seconds: float) -> None:
        """Fix the bound ``seconds`` from now (a ``time.monotonic()`` instant)."""
        self.ends_at = time.monotonic() + seconds
        self._owed: list[Callable[[], Awaitable[None]]] = []
        self._settled = False

    def seconds_left(self) -> float:
        """The seconds left before the bound ends; ``0.0`` once it has passed."""
        return max(0.0, self.ends_at - time.monotonic())

    def once_ended(self, work: Callable[[], Awaitable[None]]) -> None:
        """Owe ``work`` once the run has ended: :func:`bounded_run` awaits it at its exit.

        The run has ended whether its body returned, raised or was cancelled.

        Raises ``RuntimeError`` once the run's exit began settling what it owed — from a task
        that copied the bound and outlived the run, or from owed work itself — since work owed
        then would never run.
        """
        if self._settled:
            raise RuntimeError(
                "the bounded run has already settled the work it owed at its exit: work owed to it now would never run"
            )
        self._owed.append(work)

    async def _settle(self) -> None:
        """Await every owed work in registration order; raise what failed once each has run.

        From its start, no more work can be owed (:meth:`once_ended`). One failure is raised as
        it is; several are raised together in an ``ExceptionGroup``.
        """
        self._settled = True
        failures: list[Exception] = []
        for work in self._owed:
            try:
                await work()
            except Exception as failure:
                failures.append(failure)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("the work a bounded run owed at its exit failed", failures)


@asynccontextmanager
async def bounded_run(seconds: float) -> AsyncIterator[RunBound]:
    """Run the scope as a bounded run ending ``seconds`` from now; yield its :class:`RunBound`.

    At the exit — after the body returned or raised, cancellation included — the work the run
    owes is awaited in registration order, then the bound is reset. A raise from that work
    propagates out of the scope, with the run's own error, when it raised, as its context. A
    scope opened inside a bounded run adds no bound and yields the run's own: the run's bound is
    the outermost, so its work is owed at the run's exit, never an inner scope's.
    """
    outer = _run_bound.get()
    if outer is not None:
        yield outer
        return
    bound = RunBound(seconds)
    token = _run_bound.set(bound)
    try:
        yield bound
    finally:
        try:
            await bound._settle()
        finally:
            _run_bound.reset(token)


def run_bound() -> RunBound | None:
    """The bound the current context runs under, else ``None`` (an unbounded run)."""
    return _run_bound.get()
