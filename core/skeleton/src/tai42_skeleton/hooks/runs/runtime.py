"""The per-process hook-run runtime: the attempt registry, the stopping flag and the concurrency bound.

Every config apply drops and rebuilds the hooks manager, while an attempt begun before the apply
keeps running. So the state that must hold for ALL of this process's attempts lives here, as module
state, never on a manager: the shutdown stop reaches every attempt whichever manager generation was
current when it began, and ``HOOKS_MAX_WORKERS`` bounds them all with one count. Nothing here is
reset by a settings reset.

The manager is only the store. Every step reads :func:`manager` at that moment, and every write on
a claimed job is fenced on the claimer id the job carries, which an apply does not change.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import secrets
import socket
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_skeleton.hooks.managers.base_hooks_manager import BaseHooksManager

    from .models import HookRun

logger = logging.getLogger(__name__)

# This process's claimer id, written as ``claimed_by`` on every job it claims.
_CLAIMED_BY = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"

# Run id -> the attempt task running it in this process, waiting ones included.
_ATTEMPTS: dict[str, asyncio.Task[None]] = {}

# Set by the shutdown stop: an accept then writes its jobs unclaimed and spawns no attempt.
_STOPPING = False

# The attempts holding a slot of the per-process bound, and the waiters for one, oldest first.
_SLOTS_IN_USE = 0
_WAITERS: collections.deque[asyncio.Future[None]] = collections.deque()


def now() -> float:
    """The wall-clock time every hook-run timestamp, lease and due score is taken in."""
    return time.time()


def manager() -> BaseHooksManager:
    """The current hooks manager, read at this moment so a settings reset is followed."""
    from tai42_skeleton.hooks import cache

    return cache.get_hooks_manager()


def on_serving_loop() -> bool:
    """Whether the caller runs on this process's serving loop (a task spawned there keeps running)."""
    from tai42_skeleton.app.instance import built_app

    app = built_app()
    return app is not None and app.on_serving_loop()


def claimed_by() -> str:
    """This process's claimer id."""
    return _CLAIMED_BY


def stopping() -> bool:
    """Whether the shutdown stop has begun in this process."""
    return _STOPPING


def set_stopping(value: bool) -> None:
    """Set or clear the stopping flag (the sweep's start clears it, its stop sets it)."""
    global _STOPPING
    _STOPPING = value


def _limit() -> int:
    return manager().settings.max_workers


async def acquire_slot() -> None:
    """Take a slot of the per-process bound, or wait in line while the count is at the limit.

    The limit is read from the current settings at each admission, so a raised limit admits a new
    attempt at once and the waiting ones at the next release. A cancelled waiter leaves the line
    and takes no slot.
    """
    global _SLOTS_IN_USE
    if _limit() > _SLOTS_IN_USE:
        _SLOTS_IN_USE += 1
        return
    waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _WAITERS.append(waiter)
    try:
        await waiter
    except asyncio.CancelledError:
        if waiter in _WAITERS:
            _WAITERS.remove(waiter)
        elif waiter.done() and not waiter.cancelled():
            # Woken (a slot was handed over) at the moment it was cancelled: give the slot back.
            release_slot()
        raise


def release_slot() -> None:
    """Give a slot back, then hand slots to the waiters, oldest first, while the current limit allows."""
    global _SLOTS_IN_USE
    _SLOTS_IN_USE -= 1
    wake_waiters()


def wake_waiters() -> None:
    """Hand free slots to the waiters, oldest first, while the count is below the current limit."""
    global _SLOTS_IN_USE
    while _WAITERS and _limit() > _SLOTS_IN_USE:
        waiter = _WAITERS.popleft()
        if waiter.done():
            continue
        _SLOTS_IN_USE += 1
        waiter.set_result(None)


def spawn_attempt(job: HookRun) -> asyncio.Task[None] | None:
    """Spawn the attempt of a job this process claimed, as a new root task, and register it.

    The attempt inherits nothing from its spawner: it binds every fact it acts under from the
    job. A job whose attempt is already live in this process is never given a second one; that
    is logged as an error and nothing is spawned.
    """
    from tai42_skeleton.app.root_task import spawn_root_task

    from .attempt import run_attempt

    live = _ATTEMPTS.get(job.id)
    if live is not None and not live.done():
        logger.error("hook run %s already has a live attempt in this process; no second attempt is spawned", job.id)
        return None
    task: asyncio.Task[None] = spawn_root_task(run_attempt(job), name=f"tai-hook-run-{job.id}")
    _ATTEMPTS[job.id] = task

    def _done(finished: asyncio.Task[None]) -> None:
        if _ATTEMPTS.get(job.id) is finished:
            del _ATTEMPTS[job.id]
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            logger.error("hook run %s: its attempt task died unexpectedly", job.id, exc_info=exc)

    task.add_done_callback(_done)
    return task


def live_attempt_ids() -> frozenset[str]:
    """The run ids with an attempt live in this process."""
    return frozenset(run_id for run_id, task in _ATTEMPTS.items() if not task.done())


def attempt_count() -> int:
    """How many attempts this process runs now, waiting ones included."""
    return sum(1 for task in _ATTEMPTS.values() if not task.done())


async def drain_hook_runs(timeout: float | None = None) -> set[asyncio.Task[None]]:
    """Wait until this process runs no attempt; return the attempts still running when ``timeout`` ran out.

    The registry is re-read until no attempt is left, so attempts spawned while it waits are waited
    on too.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while True:
        tasks = {task for task in _ATTEMPTS.values() if not task.done()}
        if not tasks:
            return set()
        remaining = None if deadline is None else max(0.0, deadline - loop.time())
        _done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending and deadline is not None and loop.time() >= deadline:
            return {task for task in _ATTEMPTS.values() if not task.done()}


__all__ = [
    "acquire_slot",
    "attempt_count",
    "claimed_by",
    "drain_hook_runs",
    "live_attempt_ids",
    "manager",
    "now",
    "on_serving_loop",
    "release_slot",
    "set_stopping",
    "spawn_attempt",
    "stopping",
    "wake_waiters",
]
