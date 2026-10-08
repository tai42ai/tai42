"""The per-turn "working-on-it" (typing) refresh loop.

A channel that advertises a vendor working-on-it indicator (a ``ClassVar
working_signal_expiry_seconds``) has that indicator refreshed for the life of a bridged
turn: :func:`start` spawns one detached task per turn at the schedule seam, the task
re-asserts the indicator before it lapses, and :func:`stop` cancels it the instant the
turn's answer is about to send. The loop also stops on its own when the record leaves the
in-flight delivery states (a cross-process send) or at the hard ceiling. It is a
best-effort, fire-and-forget cosmetic: a signal failure never reaches the turn — it is
logged, counted, and after three in a row the loop stops.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings, conversations_settings

logger = logging.getLogger(__name__)

# Strong references to in-flight signal-loop tasks, keyed by ``message_id``, so one is not
# GC'd before it stops and so ``stop`` can cancel it at the first send.
_WORKING_SIGNAL_TASKS: dict[str, asyncio.Task] = {}

# While the record sits in one of these the reply has not gone out, so the indicator is
# still worth refreshing; any other status means the send happened or never will.
_IN_FLIGHT_STATUSES = frozenset({DeliveryStatus.ACCEPTED, DeliveryStatus.PENDING_DELIVERY})

# Consecutive vendor failures after which the loop stops hammering a persistent fault.
_MAX_CONSECUTIVE_FAILURES = 3


def _now() -> float:
    """Monotonic seconds, used for the loop's hard-ceiling deadline."""
    return time.monotonic()


def start(record: ConversationRecord) -> None:
    """Spawn the working-signal loop for ``record``'s turn, unless nothing should signal.

    Returns without spawning when the loop is disabled (the ceiling is 0), the record has no
    channel (the api door), the channel is not registered, the channel advertises no vendor
    indicator (``working_signal_expiry_seconds`` is ``None``/absent), or the advertised expiry is
    non-positive (a plugin misconfiguration). A second ``start`` for a ``message_id`` already
    looping cancels the earlier loop and replaces it.

    Runs synchronously in the scheduling path, AFTER the turn task is spawned, so its whole body is
    fault-proofed: the working signal is a cosmetic indicator and the turn is complete and correct
    without it, so any fault here is logged loudly and swallowed rather than failing the turn.
    """
    try:
        settings = conversations_settings()
        if settings.working_signal_max_seconds <= 0:
            return
        if record.channel is None:
            return
        from tai42_skeleton.app import instance

        try:
            channel = instance.app.channels.get(record.channel)
        except KeyError:
            logger.debug(
                "conversations: channel %s is not registered; no working signal for record %s",
                record.channel,
                record.message_id,
            )
            return
        expiry = getattr(channel, "working_signal_expiry_seconds", None)
        if expiry is None:
            return
        if expiry <= 0:
            logger.warning(
                "conversations: channel %s advertises a non-positive working_signal_expiry_seconds "
                "(%r); no working signal for record %s",
                record.channel,
                expiry,
                record.message_id,
            )
            return
        existing = _WORKING_SIGNAL_TASKS.get(record.message_id)
        if existing is not None:
            logger.debug(
                "conversations: a working-signal loop for record %s is already running; replacing it",
                record.message_id,
            )
            existing.cancel()
        task = spawn_root_task(_run_loop(record, channel, expiry, settings))
        _WORKING_SIGNAL_TASKS[record.message_id] = task
        task.add_done_callback(lambda t: _on_signal_task_done(record.message_id, t))
    except Exception:
        logger.exception(
            "conversations: starting the working-signal loop for record %s failed; the turn is unaffected",
            record.message_id,
        )


def _on_signal_task_done(message_id: str, task: asyncio.Task) -> None:
    """Retire ``message_id``'s finished loop: drop it from the registry and surface any crash.

    Pops the registry entry only when it still holds THIS task, so a cancel-and-replace ``start``
    that already stored a successor is not evicted by the loser's teardown. A loop that ended in an
    unhandled exception (never a cancellation) is logged at ERROR, so a crashed signal loop is
    visible rather than a swallowed "Task exception was never retrieved".
    """
    if _WORKING_SIGNAL_TASKS.get(message_id) is task:
        del _WORKING_SIGNAL_TASKS[message_id]
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "conversations: the working-signal loop for record %s crashed",
            message_id,
            exc_info=exc,
        )


def stop(message_id: str) -> None:
    """Cancel ``message_id``'s working-signal loop if one is running.

    The loop's ``finally`` sends the clear and the cancellation completes the task; a
    ``message_id`` with no live loop is a no-op (the loop already stopped, or it ran in
    another process).
    """
    task = _WORKING_SIGNAL_TASKS.pop(message_id, None)
    if task is not None:
        task.cancel()


async def _run_loop(record: ConversationRecord, channel: Any, expiry: float, settings: ConversationsSettings) -> None:
    """Assert the working indicator now, then re-assert it under ``expiry`` until the turn sends.

    Refreshes every ``max(expiry - margin, expiry / 2)`` seconds so each frame lands before the
    vendor lifetime lapses, re-reading the record each cycle to stop once it leaves the in-flight
    states, and never running past the hard ceiling. On stop it clears the indicator once, only if
    an assert was sent.
    """
    interval = max(expiry - settings.working_signal_refresh_margin_seconds, expiry / 2)
    ceiling_deadline = _now() + settings.working_signal_max_seconds
    consecutive_failures = 0
    active_sent = False
    try:
        while True:
            if await _signal(channel, record):
                consecutive_failures = 0
                active_sent = True
            else:
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    break
            remaining = ceiling_deadline - _now()
            await asyncio.sleep(min(interval, remaining))
            if _now() >= ceiling_deadline:
                break
            try:
                rec = await cache.get_conversations_manager().records.get_record(record.message_id)
            except Exception:
                logger.warning(
                    "conversations: reading record %s to refresh its working signal failed; stopping the signal",
                    record.message_id,
                    exc_info=True,
                )
                break
            if rec is None or rec.delivery_status not in _IN_FLIGHT_STATUSES:
                break
    finally:
        if active_sent:
            try:
                await channel.signal_working(
                    recipient=record.client_address,
                    sender_identity=record.our_identity,
                    provider_message_id=record.provider_message_id,
                    active=False,
                )
            except Exception:
                # A vendor fault clearing the indicator is logged and swallowed — a cancellation
                # (``CancelledError``, a ``BaseException``) still propagates so ``stop`` completes.
                logger.warning(
                    "conversations: clearing the working signal for record %s on channel %s failed",
                    record.message_id,
                    record.channel,
                    exc_info=True,
                )


async def _signal(channel: Any, record: ConversationRecord) -> bool:
    """Assert the working indicator once; return whether it succeeded.

    A vendor fault (``ChannelDeliveryError`` or any other) is logged at WARNING and swallowed —
    the loop is a detached cosmetic task and never lets a signal failure reach the turn.
    """
    try:
        await channel.signal_working(
            recipient=record.client_address,
            sender_identity=record.our_identity,
            provider_message_id=record.provider_message_id,
            active=True,
        )
    except Exception:
        logger.warning(
            "conversations: refreshing the working signal for record %s on channel %s failed",
            record.message_id,
            record.channel,
            exc_info=True,
        )
        return False
    return True
