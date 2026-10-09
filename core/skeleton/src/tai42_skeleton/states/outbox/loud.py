"""The loud path of a pending save that failed: a log, the operator notification, the platform event.

A failed save holds its subjects until an operator retries or discards it, so its failure is
surfaced on every channel a deployment watches.
"""

from __future__ import annotations

import logging
from typing import Any

from .metrics import outbox_metrics
from .models import OutboxRow

logger = logging.getLogger(__name__)

# The platform-event topic emitted when a pending save fails; a deployment wires its own hook on it.
STATE_OUTBOX_SAVE_FAILED_EVENT_TOPIC = "states_outbox_save_failed"


async def report_failed(row: OutboxRow, *, error_kind: str | None) -> None:
    """Report ``row``, now failed: ERROR log, operator notification, the platform event.

    ``error_kind`` is the failure's contract error kind, ``None`` when no exception caused it.
    """
    phase = row.failed_phase or "records"
    error = row.last_error or ""
    logger.error(
        "states outbox: pending save %s failed in its %s phase (run %s, subjects %s): %s",
        row.id,
        phase,
        row.run_id,
        row.subject_keys,
        error,
    )
    await notify_operators(
        f"A pending state save failed; its subjects are held until it is retried or discarded. "
        f"Save {row.id}, run {row.run_id or '-'}, phase {phase}: {error}",
        save_id=row.id,
    )
    payload: dict[str, Any] = {
        "save_id": str(row.id),
        "run_id": row.run_id,
        "phase": phase,
        "subject_keys": list(row.subject_keys),
        "error_kind": error_kind,
        "error": error,
    }
    from .apply import on_serving_loop, spawn_tracked

    if on_serving_loop():
        # The hook fan-out must never sit on the wait of the reader whose help-along failed the save.
        spawn_tracked(_emit_save_failed(payload), name=f"tai-states-outbox-failed-{row.id}")
    else:
        await _emit_save_failed(payload)


async def notify_operators(message: str, *, save_id: int) -> None:
    """Write ``message`` to the operator notification feed; a feed that cannot take it is logged and counted."""
    from tai42_skeleton.channels.notifications_sink import record_notification
    from tai42_skeleton.interactions.settings import interactions_store_configured

    if not interactions_store_configured():
        logger.error(
            "states outbox: no notification feed is configured to tell operators about pending save %s", save_id
        )
        outbox_metrics().notify_failures.inc()
        return
    try:
        await record_notification(message)
    except Exception:
        logger.exception("states outbox: the operator notification for pending save %s could not be written", save_id)
        outbox_metrics().notify_failures.inc()


async def _emit_save_failed(payload: dict[str, Any]) -> None:
    from tai42_skeleton.hooks.cache import get_hooks_manager

    try:
        await get_hooks_manager().on_event(topic=STATE_OUTBOX_SAVE_FAILED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "states outbox: failed to emit %r for pending save %s",
            STATE_OUTBOX_SAVE_FAILED_EVENT_TOPIC,
            payload["save_id"],
            exc_info=True,
        )
