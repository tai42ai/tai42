"""Ending a serve worker's event streams only after its in-flight replies are written.

Every MCP reply over HTTP rides an ``EventSourceResponse``. The response library's automatic
drain ends every such response as soon as the server's graceful stop begins, a reply not yet
written included, so the serve worker switches it off and chains its own handler behind
uvicorn's for every signal uvicorn captures. uvicorn's handler starts the stop (no new
connection, idle connections closed); on the first signal the worker also starts, on the
serving loop, the drain that waits until the serving work has answered and then sets
``AppStatus.should_exit``, the library's flag that ends the long-lived streams, so uvicorn
reaches the lifespan shutdown. A call counts as answered once its reply is written: a
streamable-HTTP call once its ``POST`` response is sent, an SSE call once its session stream
has written the reply. uvicorn's ``timeout_graceful_shutdown`` caps the whole stop.
"""

from __future__ import annotations

import asyncio
import contextvars
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
from tai42_skeleton.app.reload_gate import reload_gate

__all__ = ["arm_serve_exit", "end_streams_after_in_flight_replies"]

logger = logging.getLogger(__name__)

# Strong references to the running drain, so the loop does not collect it; it removes itself.
_drains: set[asyncio.Task[None]] = set()


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


def _drain_done(task: asyncio.Task[None]) -> None:
    _drains.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error(
            "serve exit: the drain failed, so the event streams end only at uvicorn's graceful-shutdown cap",
            exc_info=exc,
        )


def _start_drain() -> None:
    # From an empty context: a task keeps the context it was started in, and the signal
    # may interrupt a request whose context would then stay reachable.
    task = asyncio.create_task(
        end_streams_after_in_flight_replies(), name="tai-serve-exit", context=contextvars.Context()
    )
    _drains.add(task)
    task.add_done_callback(_drain_done, context=contextvars.Context())


def arm_serve_exit() -> None:
    """Chain the serve exit behind uvicorn's handler for every signal uvicorn captures.

    Called on the serving loop while uvicorn's signal capture is installed. Switches the
    response library's automatic drain off and, on the first captured signal, starts
    :func:`end_streams_after_in_flight_replies`. Raises, leaving the library's drain on,
    when a signal is not handled by a uvicorn server: the streams would then never end.
    """
    previous = {sig: _uvicorn_handler(sig) for sig in HANDLED_SIGNALS}
    loop = asyncio.get_running_loop()
    started = False

    def handle(signum: int, frame: FrameType | None) -> None:
        nonlocal started
        previous[signal.Signals(signum)](signum, frame)
        if not started:
            started = True
            loop.call_soon_threadsafe(_start_drain)

    AppStatus.disable_automatic_graceful_drain()
    for sig in previous:
        signal.signal(sig, handle)


async def end_streams_after_in_flight_replies() -> None:
    """Wait until the worker's serving work has answered, then end its event streams.

    Lets a reload in progress turn over (its retire then has drained and closed the previous
    generation, or handed it to a deferred retire tail), waits until the serving generation
    is answered (:meth:`~tai42_skeleton.app.epoch.Epoch.wait_answered`) and every deferred
    retire tail is done, and repeats until a pass finds all of it settled. No bound of its
    own: uvicorn's graceful-shutdown cap bounds the stop.
    """
    retires = 0
    while True:
        async with reload_gate.synchronized():
            pass
        epoch = current_epoch_or_none()
        if epoch is not None:
            await epoch.wait_answered()
        tails = deferred_retires()
        if tails:
            retires += len(tails)
            await asyncio.wait(tails)
            continue
        if current_epoch_or_none() is epoch and not reload_gate.locked:
            break
    logger.info("serve exit: in-flight replies written (%d deferred retire(s) awaited); ending event streams", retires)
    AppStatus.should_exit = True
