"""The background recovery sweep task lifecycle: a single per-generation loop the app boots and
tears down that periodically re-drives stalled deliveries, adopts lapsed intakes, and prunes the
terminal-status and route-thread indexes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from tai42_skeleton.conversations.records import PRUNE_START, PruneCursor
from tai42_skeleton.conversations.settings import ConversationsSettings

logger = logging.getLogger(__name__)

# The periodic recovery sweep, held so the lifespan can cancel it at shutdown.
_sweep_task: asyncio.Task[None] | None = None

# Where the last index-prune pass stopped. Carried between passes so each one resumes the
# walk instead of re-reading the head of the same indexes forever.
_prune_cursor: PruneCursor = PRUNE_START


def start_delivery_sweep() -> None:
    """(Re)start the periodic recovery sweep — stalled deliveries and lapsed intakes —
    the post-swap establisher body. Must be called ON the serving loop (the post-swap
    hook runs there at boot and after every epoch swap), so the task attaches to the loop
    its deliveries run on and retires with its generation. Cancels any previous task."""
    global _sweep_task
    task = _sweep_task
    if task is not None and not task.done():
        task.cancel()
    interval = ConversationsSettings().delivery_sweep_interval_seconds
    logger.info("conversations: sweeping for stalled deliveries and lapsed intakes every %ss", interval)
    _sweep_task = asyncio.create_task(_sweep_loop(interval), name="tai-conversations-delivery-sweep")
    _sweep_task.add_done_callback(_on_sweep_done)
    _register_sweep_with_epoch(_sweep_task)


def _register_sweep_with_epoch(task: asyncio.Task[None]) -> None:
    """Register this sweep task's cancel with the epoch under construction, so the epoch
    retire cancels exactly the generation's own sweep and no timer outlives its epoch. A
    no-op when no epoch is installed. The cancel awaits only a task on the retire's own
    loop, so a task left on a torn-down build loop is cancelled without a cross-loop await."""
    from tai42_skeleton.app.epoch import epoch_under_construction_or_none

    epoch = epoch_under_construction_or_none()
    if epoch is None:
        return

    async def _cancel() -> None:
        if task.done():
            return
        task.cancel()
        if task.get_loop() is asyncio.get_running_loop():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    epoch.register_periodic_loop(_cancel)


async def stop_delivery_sweep() -> None:
    """Cancel and await the sweep task. Must be called ON the serving loop the task lives
    on, so the await is loop-safe. Only ``CancelledError`` is suppressed."""
    global _sweep_task
    task = _sweep_task
    _sweep_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _redrive_lapsed_intakes() -> None:
    """Adopt and resolve records whose turn worker died, through the turn engine's
    lease-gated re-drive. Imported inside the call: the turn engine imports the delivery module."""
    from tai42_skeleton.conversations.turn import redrive_accepted

    await redrive_accepted()


async def _prune_terminal_indexes() -> None:
    """Drop the expired members of the terminal-status indexes that no listing reads, and
    the reclaimable members of every route's thread indexes, so neither can outgrow the
    retained keyspace it names.

    Every LIVE route is handed to the pass: a thread index the pass is not given is walked
    by nothing, so its members would grow forever. The pass is bounded, so it stops where
    the budget runs out and the cursor it returns is what the next one resumes from."""
    global _prune_cursor
    from tai42_skeleton.conversations import delivery

    routes = await delivery.get_conversations_manager().list_routes()
    _prune_cursor = await delivery._store().prune_expired_terminal_indexes(routes.keys(), _prune_cursor)


async def _sweep_loop(interval_seconds: float) -> None:
    """Run every recovery pass every ``interval_seconds`` for the life of the process. A
    failing pass is logged at ERROR and the others still run — a dead sweep is the silent
    abandonment it exists to prevent."""
    from tai42_skeleton.conversations import delivery

    passes = (
        ("stalled-delivery sweep", delivery.sweep_stalled_deliveries),
        ("lapsed-intake re-drive", _redrive_lapsed_intakes),
        ("terminal-index prune", _prune_terminal_indexes),
    )
    while True:
        await asyncio.sleep(interval_seconds)
        for name, run_pass in passes:
            try:
                await run_pass()
            except Exception:
                logger.error("conversations: %s pass failed; retrying in %ss", name, interval_seconds, exc_info=True)


def _on_sweep_done(task: asyncio.Task[None]) -> None:
    """Surface an unexpected death of the sweep task at ERROR; a cancellation is silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("conversations: stalled-delivery sweep task died unexpectedly", exc_info=exc)


__all__ = [
    "start_delivery_sweep",
    "stop_delivery_sweep",
]
