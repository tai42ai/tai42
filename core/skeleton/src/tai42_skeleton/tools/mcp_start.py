"""MCP server starts: a start outlives its caller's wait and ends at the start budget, :func:`aclose` or its loop close.

Starting an MCP server — opening its transport, launching a stdio child, listing its tools — runs as a task of the
process's serving loop, apart from the caller's own task. A caller on another loop (a backend job's private loop)
waits there for the start on the serving loop, so the start never sits on a loop that may stop running once the
caller's job ends. The caller waits its own budget (``wait``); the start is bounded by the start budget
(``lifetime``) alone. With no serving loop bound (a forked job child, a boot before the bind), the start runs as a
task of the caller's own loop, which runs only while its caller keeps it running: the caller then waits the start
out whatever its ``wait``, and a cancel of the caller is held until the start has ended, so the start never outlives
its caller's await. Cancelling a start tears its transport down, and for a stdio server that terminates the child
mid-launch: a command that fetches its package on its first run and is terminated partway can leave that package
unstartable. So a caller that stops waiting never cancels the start: it gets :class:`McpStartPending`, and the
start's outcome — its result or its exception — goes, exactly once, to an ``on_late_outcome`` hook when it arrives,
together with the :class:`StartId` naming that start — the same object the :class:`McpStartPending` carries. Only
the start budget, :func:`aclose`, or the close of the start's loop tears a start down.

One start runs per key at a time. A caller whose ``key`` names a start still in flight joins that start instead
of launching another: it waits its own wait on the same start and gets the same outcome, or the same
:class:`McpStartPending`. The callers of one key share one late-outcome hook: the hook of the first caller that
stops waiting receives the outcome, once. Once a start has ended, or its loop has closed, its key is free, and the
next caller starts again.

A start is a root of execution of its own, not a part of the request or run that asked for it: it outlives that
caller. Its task is created from the fork gate's rebuild context
(:meth:`~tai42_kit.fork_gate.ForkGate.rebuild_context`), so the start, the late-outcome hook and whatever the hook
schedules carry none of the caller's contextvars (its call chain, its identity, any scope it set), and a stdio
child it forks while a module re-import holds the fork gate is the re-import's own and does not wait on it.

:func:`aclose` at shutdown cancels every start still running on the calling loop. A forked child starts with no
start in flight: the parent's tasks do not run in it.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, final

from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.tools.mcp_health import redact_probe_error

logger = logging.getLogger(__name__)


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


class _Flight:
    """One start in flight: its task, its :class:`StartId`, and whether its late-outcome hook is handed over."""

    __slots__ = ("handed", "start_id", "task")

    def __init__(self, task: asyncio.Task[Any]) -> None:
        self.task = task
        self.start_id = StartId()
        self.handed = False

    def ended(self) -> bool:
        """Whether the start is over: its task is done, or the loop it runs on has closed and never runs it again."""
        return self.task.done() or self.task.get_loop().is_closed()


class _Flights:
    """The starts in flight, one per key; a start leaves when it ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[Hashable, _Flight] = {}

    def join_or_launch(self, key: Hashable, launch: Callable[[], asyncio.Task[Any]]) -> _Flight:
        """The start in flight for ``key``; with none, the start ``launch`` creates, in flight until it ends."""
        with self._lock:
            flight = self._by_key.get(key)
            if flight is not None and not flight.ended():
                return flight
            flight = self._by_key[key] = _Flight(launch())
        flight.task.add_done_callback(lambda _done: self._land(key, flight))
        return flight

    def take_hook_slot(self, flight: _Flight) -> bool:
        """Whether this caller hands the late-outcome hook of ``flight``: true for the first caller that asks."""
        with self._lock:
            if flight.handed:
                return False
            flight.handed = True
            return True

    def on_loop(self, loop: asyncio.AbstractEventLoop) -> list[asyncio.Task[Any]]:
        """The tasks of the starts in flight on ``loop``."""
        with self._lock:
            return [flight.task for flight in self._by_key.values() if flight.task.get_loop() is loop]

    def _land(self, key: Hashable, flight: _Flight) -> None:
        with self._lock:
            if self._by_key.get(key) is flight:
                del self._by_key[key]

    def reset_after_fork(self) -> None:
        """Start a forked child with no start in flight: the parent's starts do not run in the child."""
        self._lock = threading.Lock()
        self._by_key = {}


_flights = _Flights()
if sys.platform != "win32":
    os.register_at_fork(after_in_child=_flights.reset_after_fork)


def outcome_text(outcome: object) -> str:
    """A credential-free one-liner for a start outcome: ``listed``, or the error's type and redacted message."""
    if isinstance(outcome, BaseException):
        return f"{type(outcome).__name__}: {redact_probe_error(str(outcome))}"
    return "listed"


async def _bounded[T](run: Callable[[], Awaitable[T]], title: str, lifetime: float) -> T:
    """Run one start, torn down only when it outruns the start budget."""
    budget = asyncio.timeout(lifetime)
    try:
        async with budget:
            return await run()
    except TimeoutError as exc:
        if budget.expired():
            raise TimeoutError(f"MCP server '{title}' did not start within the start budget {lifetime}s") from exc
        raise


def _when_done(
    task: asyncio.Task[Any],
    callback: Callable[[asyncio.Task[Any]], None],
    *,
    context: contextvars.Context | None = None,
) -> None:
    """Have ``callback`` called with ``task`` on the task's own loop once it is done, from a caller on any loop.

    ``callback`` runs in ``context``; with none, in a copy of the calling context.
    """
    loop = task.get_loop()
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if context is None:
        context = contextvars.copy_context()
    if running is loop:
        task.add_done_callback(callback, context=context)
    else:
        loop.call_soon_threadsafe(functools.partial(task.add_done_callback, callback, context=context))


def _outcome[T](start: asyncio.Task[T], title: str) -> T:
    """The finished start's result; its own exception when it failed."""
    if start.cancelled():
        raise RuntimeError(f"MCP server '{title}' start cancelled before it completed")
    return start.result()


def _hand_late[T](
    start: asyncio.Task[T],
    start_id: StartId,
    title: str,
    on_late_outcome: Callable[[StartId, T | BaseException], None],
) -> None:
    """Hand ``start_id`` and the start's outcome to ``on_late_outcome`` once it arrives.

    The hook runs on the start's loop, in the start task's own context, never in the context of the caller that
    hands it.
    """

    def deliver(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            logger.info("MCP server '%s' start cancelled before it completed", title)
            return
        error = done.exception()
        try:
            on_late_outcome(start_id, error if error is not None else done.result())
        except Exception:
            logger.exception("MCP server '%s': applying the outcome of its late start failed", title)

    _when_done(start, deliver, context=start.get_context())


async def start_listing[T](
    run: Callable[[], Awaitable[T]],
    *,
    title: str,
    key: Hashable,
    wait: float | None,
    lifetime: float,
    on_late_outcome: Callable[[StartId, T | BaseException], None],
) -> T:
    """Run the start ``run`` (connect and list, owning its client) on the serving loop and wait ``wait`` for it.

    ``key`` names the start: while a start of an equal key is in flight this caller joins it and ``run`` is not
    started. ``lifetime`` bounds the start; past it the start is torn down and raises ``TimeoutError`` naming the
    start budget. ``wait`` bounds this caller only; ``None``, or a wait as long as the start budget, waits for the
    start itself. Returns the start's result, or raises its exception, when it ends within the wait. When the wait
    ends first, raises :class:`McpStartPending` carrying the start's :class:`StartId`; a caller cancelled while
    waiting stops waiting the same way. The first caller of the start that stops waiting hands its
    ``on_late_outcome``, which receives the start's :class:`StartId` and outcome once the start ends; the callers
    of one key share that one hook, so their hooks must have the same effect. The hook runs on the loop the start
    runs on, as a done-callback of its task, so it must not block. The start and the hook run from no caller's
    context: neither sees a contextvar a caller set.

    The start runs on the serving loop the built app has bound. A caller on another loop waits from there: its wait
    runs on the serving loop (:func:`~tai42_skeleton.app.lifecycle.state.run_on_serving_loop`), in the caller's
    context, and its outcome or its :class:`McpStartPending` reaches the caller on the caller's loop. With no serving
    loop bound the start runs on the caller's loop, which may never run again once the caller returns, so the caller
    waits the start out: ``wait`` does not bound it, and a cancel of the caller hands the hook, is held until the
    start has ended, then raised.
    """
    from tai42_skeleton.app.instance import bound_serving_loop
    from tai42_skeleton.app.lifecycle.state import run_on_serving_loop

    serving = bound_serving_loop()
    return await run_on_serving_loop(
        serving,
        functools.partial(
            _start_listing_here,
            run,
            title=title,
            key=key,
            wait=wait,
            lifetime=lifetime,
            on_late_outcome=on_late_outcome,
            wait_out=serving is None,
        ),
    )


async def _start_listing_here[T](
    run: Callable[[], Awaitable[T]],
    *,
    title: str,
    key: Hashable,
    wait: float | None,
    lifetime: float,
    on_late_outcome: Callable[[StartId, T | BaseException], None],
    wait_out: bool,
) -> T:
    """:func:`start_listing` on the running loop, which runs the start's task when no start of ``key`` is in flight.

    ``wait_out`` makes this caller wait the start to its end: ``wait`` does not apply, and a cancel is held until
    the start has ended.
    """

    def launch() -> asyncio.Task[T]:
        # Created from the rebuild context: the start task runs in it, and so does the late-outcome hook.
        return fork_gate.rebuild_context().run(asyncio.get_running_loop().create_task, _bounded(run, title, lifetime))

    flight = _flights.join_or_launch(key, launch)
    start: asyncio.Task[T] = flight.task
    arrived = _arrival(start)

    def stop_waiting() -> None:
        if _flights.take_hook_slot(flight):
            _hand_late(start, flight.start_id, title, on_late_outcome)

    timer = None if wait_out or wait is None or wait >= lifetime else wait
    try:
        # ``asyncio.wait`` leaves ``arrived`` alone when it times out or is cancelled, so this caller giving
        # up never reaches the start.
        await asyncio.wait({arrived}, timeout=timer)
    except asyncio.CancelledError:
        stop_waiting()
        if wait_out:
            await _held_until(arrived)
        raise
    if start.done():
        return _outcome(start, title)
    # Only a timed wait ends before the start does.
    stop_waiting()
    raise McpStartPending(title, wait if wait is not None else lifetime, lifetime, start=flight.start_id)


def _arrival(start: asyncio.Task[Any]) -> asyncio.Future[None]:
    """A future of the running loop, set once ``start`` is done, from whichever loop runs ``start``."""
    caller = asyncio.get_running_loop()
    arrived: asyncio.Future[None] = caller.create_future()

    def wake(_done: asyncio.Task[Any]) -> None:
        # A joined start may run on another loop; the caller's may be gone when it completes.
        if caller.is_closed():
            return
        try:
            caller.call_soon_threadsafe(_set_once, arrived)
        except RuntimeError:
            if not caller.is_closed():
                raise

    _when_done(start, wake)
    return arrived


async def _held_until(arrived: asyncio.Future[None]) -> None:
    """Wait until ``arrived`` is set, holding every cancel of the caller meanwhile."""
    while not arrived.done():
        try:
            # ``asyncio.wait`` never cancels ``arrived``: a cancel here only ends this one wait.
            await asyncio.wait({arrived})
        except asyncio.CancelledError:
            continue


def _set_once(arrived: asyncio.Future[None]) -> None:
    if not arrived.done():
        arrived.set_result(None)


async def aclose() -> None:
    """Cancel every start still running on the calling loop and wait for each to end.

    A start whose callers all stopped waiting logs its cancel through its late-outcome delivery. A start on another
    loop ends with that loop.
    """
    running = _flights.on_loop(asyncio.get_running_loop())
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
