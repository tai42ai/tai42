"""The one deadline of an interactive exec: when it runs out, the exec's one stop then, and reads bounded by it.

:class:`ExecDeadline` is the exec's timeout and the event-loop time it runs out at.
:class:`DeadlineStop` is the exec's one stop at that time, whether or not anything reads its
output: the stop asks the exec's runtime whether the exec is still running, stops it if so,
and gives its :class:`DeadlineVerdict`. :func:`receive_within` is a read of the exec's output
bounded by a time and ended early by the handle's wakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecDeadline:
    """An exec's ``timeout_seconds`` and the event-loop time ``at`` they run out at.

    Fixed once, as the exec starts (:meth:`from_now`), so everything that bounds the exec (its
    start, its output) shares the one deadline, however long the caller waits in between.
    """

    timeout_seconds: float
    at: float

    @classmethod
    def from_now(cls, timeout_seconds: float) -> ExecDeadline:
        """The deadline of an exec starting now on the running loop: ``timeout_seconds`` from its current time."""
        return cls(timeout_seconds=timeout_seconds, at=asyncio.get_running_loop().time() + timeout_seconds)


@dataclass(frozen=True)
class DeadlineVerdict:
    """What an exec's stop at its deadline found.

    ``ran_past`` is whether the exec was still running at its deadline, so the stop ended it
    and its output times out; ``notes`` are what the timeout carries about the stop.
    """

    ran_past: bool
    notes: tuple[str, ...] = ()


class DeadlineStop:
    """The one stop of an exec at its :class:`ExecDeadline`.

    A timer starts the stop at the deadline whether or not anything reads the exec's output,
    and a read past the deadline starts it should the timer not have run yet: whichever comes
    first, the stop runs once and every caller awaits its one :class:`DeadlineVerdict`. The
    handle disarms the timer once the exec has ended, or a caller's kill has returned, before
    the deadline.
    """

    def __init__(self, deadline: ExecDeadline, stop: Callable[[], Awaitable[DeadlineVerdict]]) -> None:
        """Arm the stop ``stop`` for ``deadline`` on the running loop."""
        self.deadline = deadline
        self._stop = stop
        self._task: asyncio.Task[DeadlineVerdict] | None = None
        # Set as the stop starts, so a reader already waiting wakes for it.
        self.started = asyncio.Event()
        self._timer = asyncio.get_running_loop().call_at(deadline.at, self.start)

    @property
    def task(self) -> asyncio.Task[DeadlineVerdict] | None:
        """The stop, once it has started; ``None`` until then."""
        return self._task

    @property
    def armed(self) -> bool:
        """Whether the timer will still start the stop at the deadline."""
        return self._task is None and not self._timer.cancelled()

    def due(self) -> bool:
        """Whether the stop has started or the deadline has passed."""
        return self._task is not None or asyncio.get_running_loop().time() >= self.deadline.at

    def start(self) -> asyncio.Task[DeadlineVerdict]:
        """The stop, started on the first call."""
        if self._task is None:
            self._timer.cancel()
            self._task = asyncio.ensure_future(self._stop())
            self.started.set()
        return self._task

    def disarm(self) -> None:
        """Cancel the timer: the deadline no longer stops the exec unless a read starts the stop."""
        self._timer.cancel()


async def receive_within(
    receive: Callable[[], Awaitable[Any]], remaining: float, wakes: Iterable[asyncio.Event]
) -> tuple[bool, Any]:
    """Wait at most ``remaining`` seconds for ``receive()``, or until one of ``wakes`` is set.

    Returns ``(True, item)`` once the item (the end of the output included) is received, and
    ``(False, None)`` when the bound passed or a wake came first. An item received in the same
    step as a wake is returned, never dropped. A wake already set is not waited on: it woke an
    earlier wait. A bound that has passed (``remaining`` at or below zero) waits on nothing:
    ``receive()`` runs one step, so an item it holds already, needing no wait, is returned.
    """
    item = asyncio.ensure_future(receive())
    waits: set[asyncio.Future[Any]] = {item}
    waits.update(asyncio.ensure_future(wake.wait()) for wake in wakes if not wake.is_set())
    try:
        if remaining > 0:
            await asyncio.wait(waits, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(0)
    finally:
        pending = [wait for wait in waits if not wait.done()]
        for wait in pending:
            wait.cancel()
        if pending:
            await asyncio.wait(pending)
    if item.done() and not item.cancelled():
        return True, item.result()
    return False, None
