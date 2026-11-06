"""The single waiting primitive for the whole suite.

Every ``wait_for`` poll a condition on a fixed interval until a deadline; on
timeout it raises with the caller's message plus the last detail the predicate
produced, so a failure says exactly what never became true. Ad-hoc
``time.sleep`` / ``asyncio.sleep`` are banned by the ruff config: a race is
either bounded by a deadline here or it is a bug.

A wait keeps its own deadline whatever the test around it has left, and a test's
timeout stays the one thing that cuts it. :func:`running_waits` says what the waits
still polling have seen, on any thread, so the failure of a test whose timeout cut
one can say so.

At a wait's own deadline an error in its message or in its last result's ``repr``
raises, as an error in a predicate does. :func:`running_waits`, which can run in a
signal handler, names such an error in its text instead."""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Any


class WaitTimeoutError(TimeoutError):
    """A ``wait_for`` deadline elapsed before the predicate returned truthy."""


class ThreadForCaller(threading.Thread):
    """A thread whose work the thread that creates it waits on: a wait on it is that caller's as well as its own."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.caller = _caller_of(threading.current_thread())


def _caller_of(thread: threading.Thread) -> threading.Thread:
    return thread.caller if isinstance(thread, ThreadForCaller) else thread


@dataclass(eq=False)
class _RunningWait:
    message: str | Callable[[], str]
    started: float
    thread: threading.Thread
    caller: threading.Thread
    last: object = None


# The waits now polling. Read from a signal handler that can interrupt a change to it, so it takes no lock.
_running: list[_RunningWait] = []


@contextlib.contextmanager
def _polling(message: str | Callable[[], str]) -> Iterator[_RunningWait]:
    thread = threading.current_thread()
    wait = _RunningWait(message, time.monotonic(), thread, _caller_of(thread))
    _running.append(wait)
    try:
        yield wait
    finally:
        _running.remove(wait)


def _text(message: str | Callable[[], str]) -> str:
    return message() if callable(message) else message


def _said(text: str, seconds: float, last: str) -> str:
    return f"{text} (after {seconds:.1f}s; last result: {last})"


def _repr_that_cannot_raise(value: object) -> str:
    try:
        return repr(value)
    except Exception as exc:
        return f"<its repr raised {type(exc).__name__}>"


def _text_that_cannot_raise(message: str | Callable[[], str]) -> str:
    try:
        return _text(message)
    except Exception as exc:
        return f"a wait whose message raised {_repr_that_cannot_raise(exc)}"


def running_waits(*, started_since: float = -math.inf, not_on: Collection[threading.Thread] = ()) -> list[str]:
    """What each wait now polling says it has seen, in the words it would time out with: those that
    started at or after the ``time.monotonic()`` moment ``started_since`` on, and for, threads outside
    ``not_on``, every one by default.

    Safe from a signal handler: it takes no lock, and an error in a wait's message or in its last result's
    ``repr`` is named in the text, not raised."""
    now = time.monotonic()
    return [
        _said(_text_that_cannot_raise(wait.message), now - wait.started, _repr_that_cannot_raise(wait.last))
        for wait in list(_running)
        if wait.started >= started_since and wait.thread not in not_on and wait.caller not in not_on
    ]


def wait_for[T](
    predicate: Callable[[], T | None],
    *,
    deadline: float,
    interval: float = 0.1,
    message: str | Callable[[], str] = "condition never became true",
) -> T:
    """Poll ``predicate`` every ``interval`` seconds until it returns a truthy
    value or ``deadline`` seconds elapse. Returns the truthy value. Raises
    :class:`WaitTimeoutError` naming the last falsy result on timeout, with
    ``message``. A callable gives it from what the wait saw by then; it can run in
    a signal handler, so it reads what the predicate kept and does no I/O.

    The predicate is expected to be cheap and side-effect free; it may raise a
    transient connection error while a service warms, which is treated as "not
    ready yet" only for the connection-refused / 503 shapes callers pass — any
    other exception propagates immediately (never silently swallowed)."""
    with _polling(message) as wait:
        while True:
            last = predicate()
            wait.last = last
            if last:
                return last
            if time.monotonic() - wait.started >= deadline:
                raise WaitTimeoutError(_said(_text(message), deadline, repr(last)))
            time.sleep(interval)  # noqa: TID251 — the sanctioned poll interval; every other sleep is banned


async def wait_for_async[T](
    predicate: Callable[[], Awaitable[T | None]],
    *,
    deadline: float,
    interval: float = 0.1,
    message: str | Callable[[], str] = "condition never became true",
) -> T:
    """Async twin of :func:`wait_for` for predicates that must ``await`` (HTTP
    probes, MCP calls, census reads)."""
    with _polling(message) as wait:
        while True:
            last = await predicate()
            wait.last = last
            if last:
                return last
            if time.monotonic() - wait.started >= deadline:
                raise WaitTimeoutError(_said(_text(message), deadline, repr(last)))
            await asyncio.sleep(interval)  # noqa: TID251 — the sanctioned poll interval; every other sleep is banned


async def wait_absent_async[T](
    probe: Callable[[], Awaitable[T | None]],
    *,
    window: float,
    interval: float = 0.25,
    message: str = "an absent condition became true",
) -> None:
    """The negative twin of :func:`wait_for_async`: poll ``probe`` every ``interval`` seconds for
    ``window`` seconds and raise :class:`AssertionError` the moment it returns a truthy value.

    The one way a spec proves something did NOT happen after the act under test (a message never
    sent, a record never written): there is no condition to wait FOR, so the window bounds the
    observation and the probe is judged on every poll, never only once at the end. It is not among
    :func:`running_waits`: while it runs it has seen nothing, which is no finding to name."""
    start = time.monotonic()
    while True:
        seen = await probe()
        if seen:
            raise AssertionError(f"{message}: {seen!r}")
        if time.monotonic() - start >= window:
            return
        await asyncio.sleep(interval)  # noqa: TID251 — the sanctioned poll interval; every other sleep is banned


def align_to_window(window_seconds: float) -> None:
    """Block until just after the next ``window_seconds`` boundary of the wall
    clock, so a fixed-window rate-limit test cannot straddle two windows. The
    limiter keys windows on ``floor(now / window)``; starting a burst right
    after a boundary gives the full window's budget before the next rollover."""
    now = time.time()
    remaining = window_seconds - (now % window_seconds)
    deadline = now + remaining + 0.05

    def past_boundary() -> bool:
        return time.time() >= deadline

    wait_for(past_boundary, deadline=window_seconds + 1.0, interval=0.02, message="window boundary never reached")
