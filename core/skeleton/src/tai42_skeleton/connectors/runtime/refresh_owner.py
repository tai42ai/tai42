"""The owner of every credential refresh in a process: the serving loop, not whoever asked.

A process has one long-lived serving loop, bound at lifespan start. The slow path of a credential
resolve (the lock, the re-check, the attempts, the write-back) runs as a task ON that loop, at most
one per connection; every caller, on whatever loop, waits for it through a shield and may stop
waiting at any moment without touching it. A caller that finds a task in flight for its connection
waits for it and then resolves again (the stored state is what ends the waiting). Where there is no
live owner — nothing bound, a forked process that inherited the parent's loop object, or once
shutdown has begun — the slow path runs inline in its caller.

The connectors engine imports nothing from the app for this: the lifespan binds and closes it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, Final

from tai42_contract.connectors.errors import ConnectorError

logger = logging.getLogger(__name__)

# How long ``aclose`` waits for the tasks it cancelled (a section in flight finishes first).
ACLOSE_TIMEOUT_SECONDS = 30.0


class ResolveAgain:
    """Returned to a caller that joined a refresh in flight: resolve once more through its own entry."""

    def __repr__(self) -> str:
        """Name the marker in logs and test output."""
        return "RESOLVE_AGAIN"


RESOLVE_AGAIN: Final = ResolveAgain()


class _Refused:
    """Returned by the owner's loop for a hand-over that lands after :func:`aclose` began."""


_REFUSED: Final = _Refused()

_loop: asyncio.AbstractEventLoop | None = None
_pid: int | None = None
_closing = False
_tasks: dict[str, asyncio.Task[Any]] = {}


def bind_to_running_loop() -> None:
    """Bind the owner to the running loop and this process (the lifespan-start hook)."""
    global _loop, _pid, _closing
    _loop = asyncio.get_running_loop()
    _pid = os.getpid()
    _closing = False


def is_live() -> bool:
    """Whether a refresh handed to the owner will run on its loop.

    A loop is bound, it is running, it was bound in this process, and :func:`aclose` has not begun.
    """
    loop = _loop
    return loop is not None and not _closing and _pid == os.getpid() and loop.is_running() and not loop.is_closed()


def _log_outcome(connection_id: str, task: asyncio.Task[Any]) -> None:
    """Every owned task logs its own failure, whoever is or is not waiting for it."""
    if _tasks.get(connection_id) is task:
        del _tasks[connection_id]
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    if isinstance(exc, ConnectorError):
        logger.warning("connectors: owned refresh of %s ended with %s: %s", connection_id, type(exc).__name__, exc)
    else:
        logger.error("connectors: owned refresh of %s failed", connection_id, exc_info=exc)


async def _on_owner_loop[T](connection_id: str, start: Callable[[], Awaitable[T]]) -> T | ResolveAgain | _Refused:
    if _closing:
        # A caller on another loop saw the owner live, then shutdown began before its hand-over ran:
        # the owner takes no new work, and the caller runs the slow path inline instead.
        logger.warning(
            "connectors: refresh of %s handed over after shutdown began; it runs inline in its caller", connection_id
        )
        return _REFUSED
    running = _tasks.get(connection_id)
    if running is not None:
        # Join the refresh in flight; its outcome reaches this caller through the stored state.
        await asyncio.wait([running])
        return RESOLVE_AGAIN
    task: asyncio.Task[T] = asyncio.ensure_future(start())
    _tasks[connection_id] = task
    task.add_done_callback(lambda done: _log_outcome(connection_id, done))
    return await asyncio.shield(task)


def _retrieve(future: asyncio.Future[Any]) -> None:
    # A caller that left stops reading the outcome; the owned task logged it already.
    if not future.cancelled():
        future.exception()


async def run[T](connection_id: str, start: Callable[[], Awaitable[T]]) -> T | ResolveAgain:
    """Run the slow path ``start()`` for ``connection_id`` as the owner's task, or inline without a live owner.

    Returns the slow path's result, or :data:`RESOLVE_AGAIN` when this caller joined a task already
    in flight for the connection. The caller's cancellation never reaches the owned task. The owned
    coroutine runs on the owner's loop and does not see the caller's context variables, so
    ``start`` must close over everything it needs. A hand-over the owner refuses because
    :func:`aclose` began after this caller found it live runs inline, as any call after that point does.
    """
    loop = _loop
    if loop is None or not is_live():
        return await start()
    if asyncio.get_running_loop() is loop:
        outcome = await _on_owner_loop(connection_id, start)
    else:
        handed: Future[T | ResolveAgain | _Refused] = asyncio.run_coroutine_threadsafe(
            _on_owner_loop(connection_id, start), loop
        )
        waiting = asyncio.wrap_future(handed)
        waiting.add_done_callback(_retrieve)
        outcome = await asyncio.shield(waiting)
    if isinstance(outcome, _Refused):
        return await start()
    return outcome


async def aclose() -> None:
    """Stop accepting work, cancel the owned tasks and wait for them, then unbind (the lifespan-shutdown hook).

    From its start the owner is not live and refuses every hand-over, so a new call runs inline. A
    task in a refresh section finishes its write-back before it honours the cancellation; one still
    running after :data:`ACLOSE_TIMEOUT_SECONDS` is logged at ERROR by connection id and left behind.
    """
    global _loop, _pid, _closing
    _closing = True
    pending = dict(_tasks)
    for task in pending.values():
        task.cancel()
    if pending:
        _done, still_running = await asyncio.wait(pending.values(), timeout=ACLOSE_TIMEOUT_SECONDS)
        for connection_id, task in pending.items():
            if task in still_running:
                logger.error(
                    "connectors: refresh of %s still running %ss after shutdown began; leaving it behind",
                    connection_id,
                    ACLOSE_TIMEOUT_SECONDS,
                )
    _tasks.clear()
    _loop = None
    _pid = None


def reset() -> None:
    """Unbind without waiting (tests)."""
    global _loop, _pid, _closing
    _loop = None
    _pid = None
    _closing = False
    _tasks.clear()
