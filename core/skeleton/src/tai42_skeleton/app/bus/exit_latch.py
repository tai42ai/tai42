"""A worker's exit latch and the exits it owes its own in-flight changes, decided under one lock.

The latch records why this process life is leaving (:attr:`ExitLatch.departing`), set once by
the decision to leave. A change this worker runs holds the worker's exit (:class:`ExitHold`)
from the change's start until the edge answering it settles: the exit the change owes runs
then, or the hold is released when it owes none. While any hold is held, a recycle op from
another worker's roll leaves the worker to its own changes and is carried out once none is
held, and the writer of one change waits for every other held change before it leaves
(:meth:`ExitLatch.settled`), so one exit follows every in-flight change's reply.

The latch and the holds share one lock, so every decision that reads one and changes the other
is one step on any thread: a decision to leave once no hold is held (:meth:`ExitLatch.pull_unless_held`)
and a hold taken unless the worker is leaving (:meth:`ExitLatch.hold_unless_departing`) never
interleave, and a waiter on any event loop is woken on its own loop.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Literal

ExitRefusalKind = Literal["already_leaving", "exit_deferred"]


@dataclass(eq=False)
class ExitHold:
    """One in-flight change's hold on this worker's exit, released once by its owner.

    ``reason`` names the change; two holds of the same change are two holds (compared by identity).
    """

    reason: str


@dataclass(frozen=True)
class ExitRefusal:
    """Why a decision to leave was not taken, and the reason it names.

    ``already_leaving``: the latch is set, ``reason`` the exit's. ``exit_deferred``: the exit is
    held by this worker's own in-flight change, ``reason`` the oldest such change, and follows
    that change's reply.
    """

    kind: ExitRefusalKind
    reason: str


class ExitLatch:
    """One process life's exit latch, its held exits oldest first, and the waiters for the holds to empty."""

    def __init__(self) -> None:
        """Start not leaving, with no hold held and no waiter."""
        self._lock = threading.Lock()
        self._departing: str | None = None
        self._held: list[ExitHold] = []
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    @property
    def departing(self) -> str | None:
        """Why this process life is leaving, ``None`` before the decision to leave."""
        with self._lock:
            return self._departing

    @property
    def oldest_hold(self) -> str | None:
        """The reason of the oldest held hold, ``None`` when none is held."""
        with self._lock:
            return self._held[0].reason if self._held else None

    def hold(self, reason: str) -> ExitHold:
        """Take a new hold for the change ``reason`` and return it to its owner, whether or not the worker leaves."""
        held = ExitHold(reason)
        with self._lock:
            self._held.append(held)
        return held

    def hold_unless_departing(self, reason: str) -> ExitHold | ExitRefusal:
        """Take a new hold for the change ``reason`` unless the worker decided to leave.

        A worker that decided to leave holds nothing and returns the ``already_leaving`` refusal.
        """
        with self._lock:
            if self._departing is not None:
                return ExitRefusal(kind="already_leaving", reason=self._departing)
            held = ExitHold(reason)
            self._held.append(held)
        return held

    def release(self, held: ExitHold) -> None:
        """Release ``held``; the last release wakes every waiter. Raises when ``held`` is not held."""
        with self._lock:
            if not any(entry is held for entry in self._held):
                raise RuntimeError(f"worker bus: {held!r} is not held — it was released already or never taken")
            self._held = [entry for entry in self._held if entry is not held]
            woken = self._waiters if not self._held else []
            if woken:
                self._waiters = []
        for loop, waiter in woken:
            loop.call_soon_threadsafe(_wake, waiter)

    async def settled(self) -> None:
        """Return once no hold is held: at once when none is, else on the last release."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if not self._held:
                return
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append((loop, waiter))
        await waiter

    def pull(self, reason: str) -> str | None:
        """Decide to leave for ``reason`` whatever is held.

        Returns ``None`` when this call set the latch, else the reason of the earlier decision.
        """
        with self._lock:
            already = self._departing
            if already is None:
                self._departing = reason
        return already

    def pull_unless_held(self, reason: str) -> ExitRefusal | None:
        """Decide to leave for ``reason`` only when not leaving yet and no hold is held.

        Returns ``None`` when this call set the latch; otherwise the refusal says why,
        ``already_leaving`` over ``exit_deferred``.
        """
        with self._lock:
            if self._departing is not None:
                return ExitRefusal(kind="already_leaving", reason=self._departing)
            if self._held:
                return ExitRefusal(kind="exit_deferred", reason=self._held[0].reason)
            self._departing = reason
        return None

    def unlatch(self) -> None:
        """Clear the latch: only for a decision whose signal could not be sent, so the worker is not leaving."""
        with self._lock:
            self._departing = None


def _wake(waiter: asyncio.Future[None]) -> None:
    """Resolve ``waiter`` on its own loop, unless its waiting call was cancelled meanwhile."""
    if not waiter.done():
        waiter.set_result(None)
