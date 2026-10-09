"""The operator doors over the states pending-save outbox: list, retry, discard.

A failed pending save holds its subjects (every reader and run entry on them is refused) until an
operator retries it — after repairing its cause — or discards it. Shared by the HTTP routes, the
CLI and the MCP projection.
"""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from tai42_skeleton.app import instance
from tai42_skeleton.operations import (
    ConflictError,
    NotFoundError,
    NotSupportedError,
    ValidationRejectedError,
)
from tai42_skeleton.operations.decorator import operation
from tai42_skeleton.operations.response_models_group_states import (
    StatePendingSave,
    StatePendingSaveCall,
    StatePendingSaveDiscarded,
    StatePendingSaveRetried,
    StatePendingSavesPage,
    StatePendingSaveSubject,
)
from tai42_skeleton.operations.states import _states_door
from tai42_skeleton.states.outbox.models import OutboxRow

_MAX_LIMIT = 200


def _states() -> Any:
    return instance.app.states


def _row_id(value: str) -> int:
    try:
        row_id = int(value)
    except (TypeError, ValueError):
        raise ValidationRejectedError(f"a pending save id is an integer, got {value!r}") from None
    if row_id < 1:
        raise ValidationRejectedError(f"a pending save id is a positive integer, got {value!r}")
    return row_id


def pending_save_view(row: OutboxRow) -> dict[str, Any]:
    """A pending save as the doors serve it: no record data, no call arguments."""
    return StatePendingSave(
        id=str(row.id),
        status=row.status,
        run_id=row.run_id,
        states=list(row.states),
        subjects=[StatePendingSaveSubject(state=s.state, subject=s.canonical) for s in row.subjects],
        calls=[StatePendingSaveCall(kind=c.kind, target=c.target) for c in row.calls],
        attempts=row.attempts,
        last_error=row.last_error,
        failed_phase=row.failed_phase,
        created_at=row.created_at,
        failed_at=row.failed_at,
    ).model_dump(mode="json")


async def _failed_or_raise(row_id: int) -> NoReturn:
    """Raise the door's refusal for a save that is not failed: 404 when gone, 409 otherwise."""
    with _states_door():
        current = await _states().get_pending_save(row_id)
    if current is None:
        raise NotFoundError(f"no pending save {row_id}")
    raise ConflictError(f"pending save {row_id} is not failed")


@operation(
    summary="List pending state saves",
    tags=["states"],
    errors=[NotSupportedError, ValidationRejectedError],
    response_model=StatePendingSavesPage,
)
async def list_state_pending_saves(
    status: Literal["outstanding", "failed"] | None = None, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    """Outstanding pending state saves newest first, with the outstanding and failed totals.

    ``status`` ``failed`` lists the failed saves only; ``outstanding`` (or none) lists every save
    still outstanding. A page holds at most ``limit`` saves; ``cursor`` continues from a page's
    ``next_cursor``.
    """
    if status not in (None, "outstanding", "failed"):
        raise ValidationRejectedError(f"status is 'outstanding' or 'failed', got {status!r}")
    if not 1 <= limit <= _MAX_LIMIT:
        raise ValidationRejectedError(f"limit is between 1 and {_MAX_LIMIT}, got {limit}")
    before = None if cursor is None else _row_id(cursor)
    with _states_door():
        rows, counts = await _states().list_pending_saves(status=status, limit=limit, before=before)
    return {
        "items": [pending_save_view(row) for row in rows],
        "next_cursor": str(rows[-1].id) if len(rows) == limit else None,
        "outstanding": sum(counts.values()),
        "failed": counts.get("failed", 0),
    }


@operation(
    summary="Retry a failed pending state save",
    tags=["states"],
    destructive=False,
    errors=[NotSupportedError, ValidationRejectedError, NotFoundError, ConflictError],
    response_model=StatePendingSaveRetried,
)
async def retry_state_pending_save(id: str) -> dict[str, Any]:  # noqa: A002 - the door's path parameter
    """Requeue a failed pending save and apply its records now; answer its status after that.

    ``applied`` when the save landed whole and is gone; ``calls`` or ``running`` while its calls
    are queued or running; ``failed`` (with ``last_error``) when it failed again.
    """
    row_id = _row_id(id)
    with _states_door():
        outcome = await _states().retry_pending_save(row_id)
    if not outcome.requeued:
        await _failed_or_raise(row_id)
    if outcome.row is None:
        return {"id": str(row_id), "status": "applied", "last_error": None}
    return {"id": str(row_id), "status": outcome.row.status, "last_error": outcome.row.last_error}


@operation(
    summary="Discard a failed pending state save",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, ValidationRejectedError, NotFoundError, ConflictError],
    response_model=StatePendingSaveDiscarded,
)
async def discard_state_pending_save(id: str) -> dict[str, Any]:  # noqa: A002 - the door's path parameter
    """Drop a failed pending save for good: its record writes and deferred calls are never applied."""
    from tai42_skeleton.access_control.user import request_identity

    row_id = _row_id(id)
    principal, _restricted = request_identity()
    with _states_door():
        discarded = await _states().discard_pending_save(row_id, principal=principal)
    if discarded is None:
        await _failed_or_raise(row_id)
    return {"discarded": str(row_id)}
