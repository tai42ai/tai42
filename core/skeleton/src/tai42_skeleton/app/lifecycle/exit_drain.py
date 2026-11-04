"""The declared exit drain: how long this process may take, once asked to stop, to finish its in-flight work and exit.

The drain is the runtime's stop phase plus the bounds the app's modules declare for their
lifespan-shutdown steps. The runtime that owns the process's stop declares its phase, once per
process: a backend process declares its backend's own drain bound
(:attr:`tai42_contract.backend.Backend.drain_timeout`: how long its worker may take to finish its
in-flight work when it stops), a CLI serve worker its server's graceful-shutdown cap. The app's
lifespan shutdown runs after that phase in every process that runs the app lifespan, its steps
one after another; a module whose shutdown step waits a grace of its own declares that grace
under its own owner name, and a step with no grace of its own declares nothing. A process whose
stop is owned by its host (an embedded app) declares no runtime phase and so announces no drain.
A worker leaving gracefully announces the declared drain on its census row with its
``state=recycling``, kept through the whole stop, and a fleet restart waits that drain plus its
own start/rejoin step for the worker to go and its replacement to join
(:mod:`tai42_skeleton.app.recycle`).

It is not the drain budget a retire uses (:mod:`tai42_skeleton.app.lifecycle.drain`): that one
bounds an in-process generation's turnover and takes the largest declared budget, because a
retire's drains wait concurrent work under one window; this one is the whole process's stop and
sums its shutdown bounds, because the shutdown's steps run one after another.
"""

from __future__ import annotations

from collections.abc import Callable


class ExitDrain:
    """The one exit drain a process declares: its runtime's stop phase plus its shutdown steps' bounds."""

    def __init__(self) -> None:
        """Start with no runtime phase and no shutdown bound declared."""
        self._seconds: Callable[[], float | None] | None = None
        self._shutdown: dict[str, Callable[[], float]] = {}

    def declare(self, seconds: Callable[[], float | None]) -> None:
        """Declare the runtime's stop phase, read at call time; ``None`` from it means the stop has no bound.

        A second declaration raises ``ValueError``: one runtime owns a process's stop.
        """
        if self._seconds is not None:
            raise ValueError("this process's exit drain is already declared")
        self._seconds = seconds

    def declare_shutdown(self, owner: str, seconds: Callable[[], float]) -> None:
        """Declare ``owner``'s bound on its lifespan-shutdown step, read at call time.

        A second declaration under one owner raises ``ValueError``.
        """
        if owner in self._shutdown:
            raise ValueError(f"a shutdown bound is already declared for {owner!r}")
        self._shutdown[owner] = seconds

    def seconds(self) -> float | None:
        """The whole stop's bound in seconds: the runtime's phase plus every declared shutdown bound.

        ``None`` when no runtime phase is declared or the runtime's stop has no bound.
        """
        runtime = None if self._seconds is None else self._seconds()
        if runtime is None:
            return None
        return runtime + sum(seconds() for seconds in self._shutdown.values())


__all__ = ["ExitDrain"]
