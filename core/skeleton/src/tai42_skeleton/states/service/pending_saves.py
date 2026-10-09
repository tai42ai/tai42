"""The operator doors over the pending-save outbox: list the outstanding saves, retry or discard a failed one.

A failed save holds its subjects until an operator acts. A retry requeues it and applies its
records at once (its calls follow on the fast path, or the next sweep pass off the serving loop);
a discard drops its writes and calls for good and releases its subjects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from tai42_skeleton.states.outbox.apply import apply_row, on_serving_loop, run_calls, spawn_tracked
from tai42_skeleton.states.outbox.loud import notify_operators
from tai42_skeleton.states.outbox.metrics import outbox_metrics
from tai42_skeleton.states.outbox.models import OutboxRow
from tai42_skeleton.states.service.base import _StatesServiceBase

logger = logging.getLogger(__name__)

PendingSavesFilter = Literal["outstanding", "failed"]


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """A retry's result.

    ``requeued`` is false when the save was not failed; ``row`` is ``None`` once it applied whole.
    """

    requeued: bool
    row: OutboxRow | None


class _PendingSavesMixin(_StatesServiceBase):
    async def list_pending_saves(
        self, *, status: PendingSavesFilter | None, limit: int, before: int | None
    ) -> tuple[list[OutboxRow], dict[str, int]]:
        """One page of outstanding saves newest first (keyset ``before`` an id), and the per-status row counts."""
        self._ensure_available()
        rows = await self._store.outbox_page(status=status, limit=limit, before=before)
        counts = {row_status: n for row_status, n, _oldest in await self._store.outbox_status_counts()}
        return rows, counts

    async def get_pending_save(self, row_id: int) -> OutboxRow | None:
        """The pending save ``row_id``, or ``None`` when no such save is outstanding."""
        self._ensure_available()
        return await self._store.outbox_row(row_id)

    async def retry_pending_save(self, row_id: int) -> RetryOutcome:
        """Requeue the failed save ``row_id`` and apply its records now; the save as it stands after.

        Not requeued when it is not failed (or gone); its row is ``None`` once it applied whole.
        """
        self._ensure_available()
        requeued = await self._store.outbox_requeue_failed(row_id)
        if requeued is None:
            return RetryOutcome(requeued=False, row=None)
        outbox_metrics().retried.inc()
        logger.info("states outbox: pending save %s requeued by an operator retry", row_id)
        if requeued.status == "pending":
            await apply_row(self, row_id)
        current = await self._store.outbox_row(row_id)
        if current is not None and current.status == "calls" and on_serving_loop():
            spawn_tracked(run_calls(self, row_id), name=f"tai-states-outbox-retry-{row_id}")
        return RetryOutcome(requeued=True, row=current)

    async def discard_pending_save(self, row_id: int, *, principal: str | None) -> OutboxRow | None:
        """Drop the failed save ``row_id`` for good, releasing its subjects; ``None`` when it is not failed."""
        self._ensure_available()
        discarded = await self._store.outbox_delete_failed(row_id)
        if discarded is None:
            return None
        outbox_metrics().discarded.inc()
        logger.warning("states outbox: pending save %s discarded by %s", row_id, principal or "-")
        await notify_operators(
            f"Pending state save {row_id} was discarded; its writes and calls will never be applied.", save_id=row_id
        )
        return discarded
