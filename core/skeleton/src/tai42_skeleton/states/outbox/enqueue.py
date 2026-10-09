"""The one small transaction before the reply: a unit's staging written durably as one pending save.

The row's keys name every staged subject (its canonical and aliases) under its state, every such
subject across states, and every candidate subject of the door the run entered through; its targets
are the conversation targets of all of them. After the insert commits, the save is applied by a
root task on the serving loop, or inline when the caller runs off it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tai42_skeleton.states.context import current_state_context

from .apply import apply_and_call, apply_row, on_serving_loop, spawn_tracked
from .keys import record_key, subject_key_of, subject_keys_of, target_of_key
from .metrics import outbox_metrics
from .models import OutboxCall, OutboxItem, OutboxSubject

if TYPE_CHECKING:
    from tai42_skeleton.states.service.base import _StatesServiceBase

logger = logging.getLogger(__name__)


def _run_id(items: list[OutboxItem], calls: list[OutboxCall]) -> str | None:
    """The run the save belongs to: the first staged write's ``run_id``, else the first call's."""
    if items:
        item = items[0]
        origin = item.write.origin if item.write is not None else item.origin
        if origin is not None and origin.run_id is not None:
            return origin.run_id
    for call in calls:
        if call.run_id is not None:
            return call.run_id
    return None


def _trace_id() -> str | None:
    from tai42_skeleton.monitoring import get_monitoring

    return get_monitoring().writer.current_trace_id()


async def insert_pending_save(
    service: _StatesServiceBase,
    items: list[OutboxItem],
    subjects: list[OutboxSubject],
    calls: list[OutboxCall],
) -> int | None:
    """Write the pending save in one transaction and return its id; ``None`` when nothing was staged."""
    if not items and not calls:
        return None
    record_keys: set[str] = set()
    subject_keys: set[str] = set()
    for staged in subjects:
        for subject in (staged.subject, staged.canonical, *staged.aliases):
            record_keys.add(record_key(staged.state, subject))
            subject_keys.add(subject_key_of(subject))
    ctx = current_state_context()
    if ctx is not None:
        subject_keys.update(subject_keys_of(ctx.candidates))
    row_id = await service._store.outbox_insert(
        record_keys=sorted(record_keys),
        subject_keys=sorted(subject_keys),
        targets=sorted({target_of_key(key) for key in record_keys | subject_keys}),
        states=sorted({staged.state for staged in subjects}),
        run_id=_run_id(items, calls),
        trace_id=_trace_id(),
        records=items,
        subjects=subjects,
        calls=calls,
    )
    outbox_metrics().enqueued.inc()
    return row_id


async def dispatch_pending_save(
    service: _StatesServiceBase, row_id: int, *, has_records: bool, has_calls: bool
) -> None:
    """Start applying the save just enqueued.

    On the serving loop a root task applies its records and runs its calls. Off it (a backend
    worker's private task loop, where a task left pending may never run) the records apply inline
    and the calls are left to the next sweep pass.
    """
    if on_serving_loop():
        spawn_tracked(apply_and_call(service, row_id, has_calls=has_calls), name=f"tai-states-outbox-{row_id}")
        return
    if has_records:
        try:
            outcome = await apply_row(service, row_id)
        except Exception:
            # The save is durable: the sweep applies it, and every reader of its subjects waits for it.
            logger.exception("states outbox: pending save %s failed to apply inline; the sweep retries it", row_id)
            return
        if outcome.outcome not in ("applied", "not_pending"):
            logger.info(
                "states outbox: pending save %s was not applied inline (%s); it is left to the next sweep pass",
                row_id,
                outcome.outcome,
            )
