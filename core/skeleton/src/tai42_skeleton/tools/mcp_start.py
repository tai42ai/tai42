"""The process-wide MCP start loop: a server start outlives its caller's wait and ends only at the start budget.

Starting an MCP server — opening its transport, launching a stdio child, listing its tools — runs here, on one
event loop of its own in the ``tai-mcp-start`` daemon thread, never on the caller's loop. The caller waits its own
budget (``wait``); the start is bounded by the start budget (``lifetime``) alone. Cancelling a start tears its
transport down, and for a stdio server that terminates the child mid-launch: a command that fetches its package on
its first run and is terminated partway can leave that package unstartable. So a caller that stops waiting never
cancels the start: it gets :class:`McpStartPending`, and the start's outcome — its result or its exception — goes,
exactly once, to an ``on_late_outcome`` hook when it arrives, together with the :class:`StartId` naming that
start — the same object the :class:`McpStartPending` carries. Only the start budget tears a start down.

One start runs per key at a time. A caller whose ``key`` names a start still in flight joins that start instead
of launching another: it waits its own wait on the same start and gets the same outcome, or the same
:class:`McpStartPending`. The callers of one key share one late-outcome hook: the hook of the first caller that
stops waiting receives the outcome, once. Once a start has ended its key is free, and the next caller starts again.

A start is a root of execution of its own, not a part of the request or run that asked for it: it outlives that
caller. It is submitted from a fresh :class:`contextvars.Context`, so the start, the late-outcome hook and whatever
the hook schedules carry none of the caller's contextvars (its call chain, its identity, any scope it set).

The loop starts on first use and is closed by :func:`aclose` at shutdown, which cancels every start still running
and closes the client pools the loop opened; a later use starts it again. A forked child starts with no loop and
no start in flight (the parent's thread does not exist in it) and starts its own on first use.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, final

from tai42_kit.clients import shutdown_all_clients

from tai42_skeleton.tools.mcp_health import redact_probe_error

logger = logging.getLogger(__name__)

_THREAD_NAME = "tai-mcp-start"


@final
class StartId:
    """Names one start: the same object rides its :class:`McpStartPending` and is handed to its late-outcome hook."""

    __slots__ = ()


class McpStartPending(Exception):  # noqa: N818 - a start still in progress, an outcome a caller records
    """The caller stopped waiting for an MCP server start that is still in progress.

    The start goes on up to the start budget ``lifetime``; its outcome reaches the caller's late-outcome hook
    with ``start``, the start's :class:`StartId`.
    """

    def __init__(self, title: str, wait: float, lifetime: float, *, start: StartId) -> None:
        """Name the server ``title``, its caller's ``wait``, the start budget ``lifetime`` and the ``start``."""
        super().__init__(title, wait, lifetime)
        self.title = title
        self.wait = wait
        self.lifetime = lifetime
        self.start = start

    def __str__(self) -> str:
        """The pending text: both budgets and that the start goes on."""
        return (
            f"MCP server '{self.title}' start still in progress after {self.wait}s; "
            f"it continues up to the start budget {self.lifetime}s"
        )


class _StartLoop:
    """The one start loop of this process and the thread running it, created on first use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def loop(self) -> asyncio.AbstractEventLoop:
        """The running start loop, started now when there is none."""
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=_serve, args=(loop,), name=_THREAD_NAME, daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def detach(self) -> tuple[asyncio.AbstractEventLoop, threading.Thread] | None:
        """Take the running loop and its thread out of use; the next :meth:`loop` starts a fresh one."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return None
        return loop, thread

    def reset_after_fork(self) -> None:
        """Start a forked child with no start loop: the parent's thread does not exist in the child."""
        self._lock = threading.Lock()
        self._loop = self._thread = None


def _serve(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        loop.close()


class _Flight:
    """One start in flight: its future, its :class:`StartId`, and whether its late-outcome hook is handed over."""

    __slots__ = ("future", "handed", "start_id")

    def __init__(self, future: concurrent.futures.Future[Any]) -> None:
        self.future = future
        self.start_id = StartId()
        self.handed = False


class _Flights:
    """The starts in flight, one per key; a start leaves when it ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[Hashable, _Flight] = {}

    def join_or_launch(self, key: Hashable, launch: Callable[[], concurrent.futures.Future[Any]]) -> _Flight:
        """The start in flight for ``key``; with none, the start ``launch`` submits, in flight until it ends."""
        with self._lock:
            flight = self._by_key.get(key)
            if flight is not None and not flight.future.done():
                return flight
            flight = self._by_key[key] = _Flight(launch())
        flight.future.add_done_callback(lambda _done: self._land(key, flight))
        return flight

    def take_hook_slot(self, flight: _Flight) -> bool:
        """Whether this caller hands the late-outcome hook of ``flight``: true for the first caller that asks."""
        with self._lock:
            if flight.handed:
                return False
            flight.handed = True
            return True

    def _land(self, key: Hashable, flight: _Flight) -> None:
        with self._lock:
            if self._by_key.get(key) is flight:
                del self._by_key[key]

    def reset_after_fork(self) -> None:
        """Start a forked child with no start in flight: the parent's starts do not run in the child."""
        self._lock = threading.Lock()
        self._by_key = {}


_runner = _StartLoop()
_flights = _Flights()
if sys.platform != "win32":
    os.register_at_fork(after_in_child=_runner.reset_after_fork)
    os.register_at_fork(after_in_child=_flights.reset_after_fork)


def outcome_text(outcome: object) -> str:
    """A credential-free one-liner for a start outcome: ``listed``, or the error's type and redacted message."""
    if isinstance(outcome, BaseException):
        return f"{type(outcome).__name__}: {redact_probe_error(str(outcome))}"
    return "listed"


async def _bounded[T](run: Callable[[], Awaitable[T]], title: str, lifetime: float) -> T:
    """Run one start on the start loop, torn down only when it outruns the start budget."""
    budget = asyncio.timeout(lifetime)
    try:
        async with budget:
            return await run()
    except TimeoutError as exc:
        if budget.expired():
            raise TimeoutError(f"MCP server '{title}' did not start within the start budget {lifetime}s") from exc
        raise


def _outcome[T](start: concurrent.futures.Future[T], title: str) -> T:
    """The finished start's result; its own exception when it failed."""
    if start.cancelled():
        raise RuntimeError(f"MCP server '{title}' start cancelled: the MCP start loop closed")
    return start.result()


def _hand_late[T](
    start: concurrent.futures.Future[T],
    start_id: StartId,
    title: str,
    on_late_outcome: Callable[[StartId, T | BaseException], None],
) -> None:
    """Hand ``start_id`` and the start's outcome to ``on_late_outcome`` once it arrives (on the start loop's thread)."""

    def deliver(done: concurrent.futures.Future[T]) -> None:
        if done.cancelled():
            logger.info("MCP server '%s' start cancelled before it completed: the MCP start loop closed", title)
            return
        error = done.exception()
        try:
            on_late_outcome(start_id, error if error is not None else done.result())
        except Exception:
            logger.exception("MCP server '%s': applying the outcome of its late start failed", title)

    start.add_done_callback(deliver)


async def start_listing[T](
    run: Callable[[], Awaitable[T]],
    *,
    title: str,
    key: Hashable,
    wait: float | None,
    lifetime: float,
    on_late_outcome: Callable[[StartId, T | BaseException], None],
) -> T:
    """Run the start ``run`` (connect and list, owning its client) on the start loop and wait ``wait`` for it.

    ``key`` names the start: while a start of an equal key is in flight this caller joins it and ``run`` is not
    started. ``lifetime`` bounds the start; past it the start is torn down and raises ``TimeoutError`` naming the
    start budget. ``wait`` bounds this caller only; ``None``, or a wait as long as the start budget, waits for the
    start itself. Returns the start's result, or raises its exception, when it ends within the wait. When the wait
    ends first, raises :class:`McpStartPending` carrying the start's :class:`StartId`; a caller cancelled while
    waiting stops waiting the same way. The first caller of the start that stops waiting hands its
    ``on_late_outcome``, which receives the start's :class:`StartId` and outcome once the start ends; the callers
    of one key share that one hook, so their hooks must have the same effect. The hook runs on the start loop's
    thread, so it must not block. The start and the hook run from no caller's context: neither sees a contextvar
    a caller set.
    """

    def launch() -> concurrent.futures.Future[T]:
        # Submitted from a fresh context: the start task, its done-callbacks and the hook all run in a copy of it.
        return contextvars.Context().run(
            asyncio.run_coroutine_threadsafe, _bounded(run, title, lifetime), _runner.loop()
        )

    flight = _flights.join_or_launch(key, launch)
    start: concurrent.futures.Future[T] = flight.future
    caller = asyncio.get_running_loop()
    arrived: asyncio.Future[None] = caller.create_future()

    def wake(_done: concurrent.futures.Future[Any]) -> None:
        # The caller's loop may be a throwaway one already gone when a late start completes.
        if caller.is_closed():
            return
        try:
            caller.call_soon_threadsafe(_set_once, arrived)
        except RuntimeError:
            if not caller.is_closed():
                raise

    def stop_waiting() -> None:
        if _flights.take_hook_slot(flight):
            _hand_late(start, flight.start_id, title, on_late_outcome)

    start.add_done_callback(wake)
    timer = None if wait is None or wait >= lifetime else wait
    try:
        # ``asyncio.wait`` leaves ``arrived`` alone when it times out or is cancelled, so this caller giving
        # up never reaches the start.
        await asyncio.wait({arrived}, timeout=timer)
    except asyncio.CancelledError:
        stop_waiting()
        raise
    if start.done():
        return _outcome(start, title)
    # Only a timed wait ends before the start does.
    stop_waiting()
    raise McpStartPending(title, wait if wait is not None else lifetime, lifetime, start=flight.start_id)


def _set_once(arrived: asyncio.Future[None]) -> None:
    if not arrived.done():
        arrived.set_result(None)


async def _drain() -> None:
    """On the start loop: cancel every start still running, then close the client pools this loop opened."""
    current = asyncio.current_task()
    running = [task for task in asyncio.all_tasks() if task is not current]
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    try:
        await shutdown_all_clients()
    except Exception:
        logger.exception("MCP start loop: closing its client pools failed")


async def aclose() -> None:
    """Close the start loop: cancel every start still running, close its client pools, and join its thread.

    A no-op when no start loop is running. A later start starts the loop again.
    """
    detached = _runner.detach()
    if detached is None:
        return
    loop, thread = detached
    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_drain(), loop))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join)
