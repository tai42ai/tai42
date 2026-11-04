"""An asyncio event loop on a virtual clock that jumps to its next timer whenever the loop would wait.

A test that asserts how long a bounded wait takes runs on this loop to take the host's
speed out of the measure: ``loop.time()`` starts at zero and moves only when every task is
blocked on a timer, straight to that timer's deadline. ``asyncio.sleep``, ``asyncio.wait``
and ``asyncio.timeout`` all read the loop's clock, so a sleep of 0.8 s takes exactly 0.8 s
of loop time and no wall-clock time, however loaded the host is.

The loop jumps only when no callback is ready and no I/O is pending, so it suits work whose
waits are on the loop's own timers. Work waiting on a thread or a socket is not something
it can fast-forward: such a wait would let a pending timer fire first.
"""

from __future__ import annotations

import asyncio
import selectors
from collections.abc import Callable, Coroutine
from typing import Any


class _Clock:
    """The loop's virtual time, in seconds, moved forward only by the selector's jumps."""

    def __init__(self) -> None:
        self.now = 0.0


class _JumpingSelector(selectors.DefaultSelector):
    """A selector that, instead of waiting ``timeout`` seconds for I/O, advances the virtual clock by it.

    It polls its descriptors without waiting first, so ready I/O (the loop's own wake-up
    pipe included) is served before any jump. With no timer scheduled (``timeout is None``)
    it waits for real I/O, as there is no deadline to jump to.
    """

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self._clock = clock

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = super().select(0)
        if ready:
            return ready
        if timeout is None:
            return super().select(None)
        self._clock.now += timeout
        return []


class VirtualClockEventLoop(asyncio.SelectorEventLoop):
    """A selector event loop whose ``time()`` is virtual and jumps to each timer's deadline when idle."""

    def __init__(self) -> None:
        self._clock = _Clock()
        super().__init__(_JumpingSelector(self._clock))

    def time(self) -> float:
        return self._clock.now


def run_on_virtual_clock(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run ``scenario`` to completion on a fresh :class:`VirtualClockEventLoop`."""
    with asyncio.Runner(loop_factory=VirtualClockEventLoop) as runner:
        runner.run(scenario())
