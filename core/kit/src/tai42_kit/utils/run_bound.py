"""Marker for a bounded run — one that ends at a bound fixed before it started.

A dispatched backend job runs under its job budget, which no backend can lengthen
once the job runs, and a live caller's turn runs under the turn budget
(``TAI_TURN_TIMEOUT_SECONDS``) once that budget is armed. Work whose length the run
does not control — a fleet recycle roll, which waits each target's announced drain —
reads :func:`in_bounded_run` and carries itself somewhere no such bound cuts it.
Homed in kit so both the skeleton that reads it and the execution backends below the
skeleton (which never import tai42-skeleton) can set it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_bounded_run: ContextVar[bool] = ContextVar("tai42_bounded_run", default=False)


@contextmanager
def bounded_run() -> Iterator[None]:
    """Flag the current context as a bounded run for the scope's duration, resetting it after."""
    token = _bounded_run.set(True)
    try:
        yield
    finally:
        _bounded_run.reset(token)


def in_bounded_run() -> bool:
    """Whether the current context runs under a bound fixed before it started."""
    return _bounded_run.get()
