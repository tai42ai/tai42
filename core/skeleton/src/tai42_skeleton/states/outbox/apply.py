"""Applying a pending save: its record part exactly once, its deferred calls after it, at least once.

``apply_row`` applies a row's records in ONE transaction that also removes the row (or moves it to
its calls phase), after taking every record key's advisory lock in sorted order and finding no
older row with unapplied records on those keys (per-key FIFO). ``run_calls`` runs a row's calls
under a leased claim that waits for every older outstanding row on its subjects. A failure is
retried with a backoff when transient; a deterministic one, or the last attempt, fails the row
loudly and holds its subjects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from psycopg import OperationalError
from psycopg.errors import LockNotAvailable
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.monitoring import TraceContext

from tai42_skeleton.states.db import states_settings
from tai42_skeleton.states.service.staging import staged_from_outbox

from .calls import deferred_call_kind
from .loud import report_failed
from .metrics import outbox_metrics
from .models import OutboxRow, RowApply

if TYPE_CHECKING:
    from tai42_contract.states.models import ApplyResult

    from tai42_skeleton.states.service.base import _StatesServiceBase
    from tai42_skeleton.states.store import PostgresStatesStore

logger = logging.getLogger(__name__)

_TRANSIENT_KINDS = frozenset({ErrorKind.TIMED_OUT, ErrorKind.UNAVAILABLE, ErrorKind.DELIVERY_FAILED})

# The fast-path and failure-emit tasks this process spawned on its serving loop; shutdown awaits them.
_in_flight: set[asyncio.Task[Any]] = set()


def is_transient(exc: BaseException) -> bool:
    """Whether a failure may pass on a retry: a timeout, an unavailable dependency, a lost connection."""
    return isinstance(exc, OperationalError) or error_kind(exc) in _TRANSIENT_KINDS


def on_serving_loop() -> bool:
    """Whether the caller runs on this process's serving loop (a task spawned there keeps running)."""
    from tai42_skeleton.app.instance import built_app

    app = built_app()
    return app is not None and app.on_serving_loop()


def spawn_tracked(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    """Spawn ``coro`` as a root task tracked for shutdown."""
    # The app package imports the states service; it is reached at call time.
    from tai42_skeleton.app.root_task import spawn_root_task

    task = spawn_root_task(coro, name=name)
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)
    return task


def in_flight_tasks() -> set[asyncio.Task[Any]]:
    """The tracked tasks still running."""
    return {task for task in _in_flight if not task.done()}


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# -- the record part --------------------------------------------------------
async def apply_row(service: _StatesServiceBase, row_id: int, *, deadline: float | None = None) -> RowApply:
    """Apply row ``row_id``'s record part, first applying every older row it is ordered behind.

    ``deadline`` (a ``monotonic()`` instant) bounds every lock wait of a reader's help-along;
    without one the wait is bounded by ``STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS``.
    """
    while True:
        older = await _apply_once(service, row_id, deadline)
        if isinstance(older, RowApply):
            return older
        older_id, older_status = older
        if older_status != "pending":
            return RowApply("held", held_by=older_id)
        first = await apply_row(service, older_id, deadline=deadline)
        if first.outcome == "held":
            return first
        if first.outcome == "failed":
            return RowApply("held", held_by=older_id)
        if first.outcome in ("retry", "contended"):
            return RowApply(first.outcome, error=first.error)


def _lock_timeout(deadline: float | None) -> float:
    if deadline is None:
        return states_settings().outbox_drain_timeout_seconds
    return max(0.001, deadline - monotonic())


async def _apply_once(service: _StatesServiceBase, row_id: int, deadline: float | None) -> RowApply | tuple[int, str]:
    """One attempt in one transaction; an older unapplied row on the keys is returned instead."""
    store = service._store
    keys = await store.outbox_record_keys(row_id)
    if keys is None:
        return RowApply("not_pending")
    row: OutboxRow | None = None
    try:
        async with store.begin() as conn:
            await store.outbox_lock_keys(conn, keys, _lock_timeout(deadline))
            older = await store.outbox_older_unapplied(conn, row_id, keys)
            if older is not None:
                return older
            row = await store.outbox_lock_row(conn, row_id)
            if row is None or row.status != "pending":
                return RowApply("not_pending")
            writes, records = staged_from_outbox(row.records, row.subjects)
            results = await service._commit_writes(writes, staged=records, conn=conn)
            await store.outbox_finish_records(conn, row_id, has_calls=bool(row.calls))
    except LockNotAvailable as exc:
        outbox_metrics().lock_waits.inc()
        logger.info("states outbox: pending save %s waited past its lock timeout; it stays pending", row_id)
        return RowApply("contended", error=exc)
    except Exception as exc:
        return await _attempt_failed(store, row_id, "records", exc, claim=None)
    metrics = outbox_metrics()
    metrics.applied.labels("records").inc()
    metrics.apply_latency_seconds.observe((datetime.now(UTC) - row.created_at).total_seconds())
    _report_divergence(row, [r.provisional for r in records], results)
    return RowApply("applied")


def _report_divergence(row: OutboxRow, provisional: list[ApplyResult], applied: list[ApplyResult]) -> None:
    """Log, count and emit where the applied answer differs from the one the run was served."""
    diverged = [
        {"index": i, "field": field, "staged": getattr(staged, field), "applied": getattr(landed, field)}
        for i, (staged, landed) in enumerate(zip(provisional, applied, strict=True))
        for field in ("applied", "skipped")
        if getattr(staged, field) != getattr(landed, field)
    ]
    if not diverged:
        return
    logger.warning("states outbox: pending save %s diverged from its projection: %s", row.id, diverged)
    outbox_metrics().divergences.inc()
    if row.trace_id is None:
        return
    from tai42_skeleton.monitoring import get_monitoring

    get_monitoring().writer.create_event(
        name="state:pending-save-divergence",
        trace_context=TraceContext(trace_id=row.trace_id),
        output={"outbox_id": str(row.id), "divergences": diverged},
    )


async def _attempt_failed(
    store: PostgresStatesStore, row_id: int, phase: str, exc: BaseException, *, claim: str | None
) -> RowApply:
    """Record one failed attempt: back off when transient and attempts remain, else fail the row loudly."""
    transient = is_transient(exc)
    outcome = await store.outbox_record_failure(
        row_id,
        phase=phase,
        error=_error_text(exc),
        transient=transient,
        max_attempts=states_settings().outbox_max_attempts,
        retry_base_seconds=states_settings().outbox_retry_base_seconds,
        retry_cap_seconds=states_settings().outbox_retry_cap_seconds,
        claim=claim,
    )
    metrics = outbox_metrics()
    metrics.attempt_failures.labels(phase, "transient" if transient else "deterministic").inc()
    if outcome is None:
        logger.error(
            "states outbox: pending save %s failed in its %s phase and moved on before the failure was recorded: %s",
            row_id,
            phase,
            _error_text(exc),
            exc_info=exc,
        )
        return RowApply("not_pending", error=exc)
    status, attempts = outcome
    if status != "failed":
        logger.warning(
            "states outbox: pending save %s failed attempt %d in its %s phase; retrying after a backoff: %s",
            row_id,
            attempts,
            phase,
            _error_text(exc),
        )
        return RowApply("retry", error=exc)
    metrics.failed.labels(phase).inc()
    failed = await store.outbox_row(row_id)
    if failed is not None:
        await report_failed(failed, error_kind=error_kind(exc).value)
    return RowApply("failed", held_by=row_id, error=exc)


# -- the calls part ---------------------------------------------------------
class _ClaimLostError(Exception):
    """The calls claim was taken over by another runner."""


async def run_calls(service: _StatesServiceBase, row_id: int) -> None:
    """Run row ``row_id``'s deferred calls in order under a leased claim, then delete the row.

    No claim (another runner holds it, an older row on its subjects is outstanding, or it is not
    due) is a no-op. A call a lapsed claim interrupted is re-run only when its kind reports it
    resumable; otherwise the row fails.
    """
    store = service._store
    me = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    lease = states_settings().outbox_claim_lease_seconds
    claimed = await store.outbox_claim_calls(row_id, me, lease)
    if claimed is None:
        return
    previous, row = claimed
    if previous == "running" and row.calls_done < len(row.calls):
        interrupted = row.calls[row.calls_done]
        if not await deferred_call_kind(interrupted.kind).resumable(interrupted.payload):
            await _fail_interrupted(store, row_id, interrupted.target, me)
            return
    await _run_under_claim(store, row, me, lease)


async def _fail_interrupted(store: PostgresStatesStore, row_id: int, target: str, claim: str) -> None:
    """Fail a row whose call a process exit interrupted and whose kind cannot re-run it."""
    error = f"deferred call {target!r} was interrupted by a process exit and its tool does not declare crash-resume"
    if await store.outbox_fail(row_id, phase="calls", error=error, claim=claim):
        outbox_metrics().failed.labels("calls").inc()
        failed = await store.outbox_row(row_id)
        if failed is not None:
            await report_failed(failed, error_kind=None)


async def _run_under_claim(store: PostgresStatesStore, row: OutboxRow, claim: str, lease: float) -> None:
    """Run the calls in their own task while a heartbeat holds the claim; a lost claim cancels them."""
    calls = asyncio.create_task(_run_claimed(store, row, claim), name=f"tai-states-outbox-calls-{row.id}")
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(store, row.id, claim, lease, calls, lost))
    try:
        await calls
    except asyncio.CancelledError:
        if not lost.is_set():
            raise
        logger.exception("states outbox: pending save %s lost its calls claim; its call was cancelled", row.id)
    except _ClaimLostError:
        logger.exception("states outbox: pending save %s lost its calls claim to another runner", row.id)
    except Exception as exc:
        await _attempt_failed(store, row.id, "calls", exc, claim=claim)
    finally:
        heartbeat.cancel()
        if not calls.done():
            calls.cancel()


async def _run_claimed(store: PostgresStatesStore, row: OutboxRow, claim: str) -> None:
    from .drain import outbox_apply_scope

    subject_keys = frozenset(row.subject_keys)
    for index in range(row.calls_done, len(row.calls)):
        call = row.calls[index]
        kind = deferred_call_kind(call.kind)
        with outbox_apply_scope(row.id, subject_keys, claim):
            await kind.apply(call.payload, idempotency_key=f"{row.id}:{index}")
        if not await store.outbox_call_done(row.id, claim):
            raise _ClaimLostError
    if not await store.outbox_delete_claimed(row.id, claim):
        raise _ClaimLostError
    outbox_metrics().applied.labels("calls").inc()


async def _heartbeat(
    store: PostgresStatesStore,
    row_id: int,
    claim: str,
    lease: float,
    calls: asyncio.Task[None],
    lost: asyncio.Event,
) -> None:
    """Extend the claim's lease every third of it; a lost claim cancels the calls."""
    while not calls.done():
        await asyncio.sleep(lease / 3)
        if calls.done():
            return
        if not await store.outbox_heartbeat(row_id, claim, lease):
            lost.set()
            calls.cancel()
            return


# -- the fast path ----------------------------------------------------------
async def apply_and_call(service: _StatesServiceBase, row_id: int, *, has_calls: bool) -> None:
    """The fast path's root task: apply the record part, then run the calls in this task."""
    try:
        await apply_row(service, row_id)
        if has_calls:
            await run_calls(service, row_id)
    except Exception:
        logger.exception("states outbox: pending save %s failed on the fast path; the sweep retries it", row_id)
