"""Ending a serve worker's event streams only after its in-flight replies are written.

Every MCP reply over HTTP rides an ``EventSourceResponse``. The response library's automatic
drain ends every such response as soon as the server's graceful stop begins, a reply not yet
written included, so the serve worker switches it off and chains its own handler behind
uvicorn's for every signal uvicorn captures. uvicorn's handler starts the stop (no new
connection, idle connections closed) on its main loop's next tick, when it closes its
listeners; on the first signal the worker also begins its leaving on the serving loop — the
census reads the stop as ``recycling`` from its start — and starts there the drain that
waits until those listeners are closed, the serving work has answered and the tool runs it
admitted in this process have ended, and then sets ``AppStatus.should_exit``, the library's
flag that ends the long-lived streams. The drain is one of the server's tasks, the ones
uvicorn's stop waits for before the lifespan shutdown, so that shutdown — which cancels every
tool run still in flight — runs after it, a run answered with ``202`` and no open connection
included. A connection accepted between the signal and that tick is
served like any other: its reply is written before the streams end. A call counts as
answered once its reply is written: a streamable-HTTP call once its ``POST`` response is
sent, an SSE call once its session stream has written the reply. uvicorn's
``timeout_graceful_shutdown`` caps the server's own stop phase (its connections, in-flight
replies and streams): at the cap uvicorn cancels its tasks, the drain included, and the
lifespan shutdown cuts what is still running. The app's lifespan shutdown runs after that
phase, outside the cap, within the bounds its modules declare. The exit drain the serve worker
announces on leaving, which a fleet restart waits for, is the cap plus those bounds.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import signal
from collections.abc import Callable
from types import FrameType, MethodType
from typing import Any

import uvicorn
from sse_starlette.sse import AppStatus
from uvicorn.server import HANDLED_SIGNALS

from tai42_skeleton.app.epoch import current_epoch_or_none
from tai42_skeleton.app.epoch_retire import deferred_retires
from tai42_skeleton.app.lifecycle.exit_drain import ExitDrain
from tai42_skeleton.app.reload_gate import reload_gate

__all__ = ["arm_serve_exit", "end_streams_after_in_flight_replies"]

logger = logging.getLogger(__name__)

# Strong references to the running drain, so the loop does not collect it; it removes itself.
_drains: set[asyncio.Task[None]] = set()

# How often the drain looks whether uvicorn has closed its listeners: uvicorn's own main loop
# ticks at this interval and closes them on the first tick after its handler flagged the stop.
_LISTENERS_POLL_S = 0.1


def _uvicorn_handler(sig: signal.Signals) -> Callable[[int, FrameType | None], Any]:
    """The handler a uvicorn server's signal capture installed for ``sig``; raises when another one is installed.

    The capture installs the server's bound ``handle_exit``: the response library's replacement
    when the library was imported first, uvicorn's own function when the capture ran before the
    library's import replaced it on the class (a worker uvicorn spawns imports the app, and the
    library with it, inside its capture). With the library's automatic drain off, its
    replacement only calls uvicorn's function, so chaining behind either is the same stop.
    """
    handler = signal.getsignal(sig)
    if (
        not isinstance(handler, MethodType)
        or not isinstance(handler.__self__, uvicorn.Server)
        or handler.__func__ not in (type(handler.__self__).handle_exit, AppStatus.original_handler)
    ):
        raise RuntimeError(
            f"the serve exit cannot be armed: {sig.name} is handled by {handler!r}, not by a uvicorn "
            "server's signal capture; it is armed from the uvicorn worker factory, which uvicorn runs "
            "in the main thread inside its signal capture"
        )
    return handler


def _drain_done(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    _drains.discard(task)
    server.server_state.tasks.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error(
            "serve exit: the drain failed, so the event streams end only at uvicorn's graceful-shutdown cap",
            exc_info=exc,
        )


def _start_drain(server: uvicorn.Server) -> None:
    # From an empty context: a task keeps the context it was started in, and the signal
    # may interrupt a request whose context would then stay reachable.
    task = asyncio.create_task(
        end_streams_after_in_flight_replies(server), name="tai-serve-exit", context=contextvars.Context()
    )
    _drains.add(task)
    # One of the server's tasks, so uvicorn's stop waits for it, within its graceful-shutdown
    # cap, before the lifespan shutdown.
    server.server_state.tasks.add(task)
    task.add_done_callback(functools.partial(_drain_done, server), context=contextvars.Context())


def arm_serve_exit(exit_drain: ExitDrain, stop_began: Callable[[str], object]) -> None:
    """Chain the serve exit behind uvicorn's handler for every signal uvicorn captures.

    Called on the serving loop while uvicorn's signal capture is installed. Switches the
    response library's automatic drain off and, on the first captured signal, calls
    ``stop_began`` with the signal's name — the worker's observation of the stop's start
    (:meth:`~tai42_skeleton.app.lifecycle.bus_subscription.BusSubscriptionMixin.stop_began`) —
    and starts :func:`end_streams_after_in_flight_replies`, both on the serving loop. Declares
    the server's graceful-shutdown cap, read at call time, as the runtime phase of this
    process's ``exit_drain``: the cap bounds the server's own stop phase, and the app's
    lifespan-shutdown bounds add to it in the announced drain (``None``, uvicorn's no cap,
    declares a stop with no bound). Raises, leaving the library's drain on,
    when a signal is not handled by a uvicorn server: the streams would then never end.
    """
    previous = {sig: _uvicorn_handler(sig) for sig in HANDLED_SIGNALS}
    # The server a stop signal stops.
    server: uvicorn.Server = previous[signal.SIGTERM].__self__
    exit_drain.declare(lambda: server.config.timeout_graceful_shutdown)
    loop = asyncio.get_running_loop()
    started = False

    def handle(signum: int, frame: FrameType | None) -> None:
        nonlocal started
        handler = previous[signal.Signals(signum)]
        handler(signum, frame)
        if not started:
            started = True
            loop.call_soon_threadsafe(stop_began, f"{signal.Signals(signum).name} received")
            loop.call_soon_threadsafe(_start_drain, handler.__self__)

    AppStatus.disable_automatic_graceful_drain()
    for sig in previous:
        signal.signal(sig, handle)


async def end_streams_after_in_flight_replies(server: uvicorn.Server) -> None:
    """Wait until ``server`` stops accepting and the serving work has answered and ended, then end the event streams.

    First waits until ``server`` has started and every listener of it is closed, so no request
    reaches the worker after its streams end: a signal during the lifespan startup arrives before
    uvicorn opened its listeners, and uvicorn then finishes its startup and closes them at once.
    Then lets a reload in progress turn over (its retire then has
    drained and closed the previous generation, or handed it to a deferred retire tail),
    waits until the serving generation is answered
    (:meth:`~tai42_skeleton.app.epoch.Epoch.wait_answered`), every tool run it admitted in this
    process has ended (:func:`~tai42_skeleton.operations.tool_runs.wait_supervisors`, which
    cancels nothing) and every deferred retire tail is done, and repeats until a pass finds all
    of it settled. No bound of its own: uvicorn's graceful-shutdown cap bounds the stop.
    """
    while not server.started or any(listener.is_serving() for listener in server.servers):
        await asyncio.sleep(_LISTENERS_POLL_S)
    retires = 0
    while True:
        async with reload_gate.synchronized():
            pass
        epoch = current_epoch_or_none()
        if epoch is not None:
            await epoch.wait_answered()
            # Imported here: the tool-runs package is rebuilt by a reload, and its serving
            # generation's registry holds the runs this generation admitted.
            from tai42_skeleton.operations.tool_runs import wait_supervisors

            await wait_supervisors(epoch=epoch.number)
        tails = deferred_retires()
        if tails:
            retires += len(tails)
            await asyncio.wait(tails)
            continue
        if current_epoch_or_none() is epoch and not reload_gate.locked:
            break
    logger.info("serve exit: in-flight replies written (%d deferred retire(s) awaited); ending event streams", retires)
    AppStatus.should_exit = True
