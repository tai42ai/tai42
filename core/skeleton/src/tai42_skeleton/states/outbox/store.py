"""The ``state_outbox`` table — the statements the enqueue, the applier, the drains, the sweep and the doors run.

Composed into :class:`~tai42_skeleton.states.store.PostgresStatesStore`, so every statement runs on
the records' database through the store's own connection seam: the apply of a row's records and
the row's removal commit in ONE transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tai42_skeleton.states.store.base import _StoreBase

from .models import OutboxCall, OutboxItem, OutboxRow, OutboxSubject

_COLUMNS = (
    "id, status, record_keys, subject_keys, targets, states, run_id, trace_id, records, subjects, calls, "
    "calls_done, attempts, next_attempt_at, claimed_by, lease_until, last_error, failed_phase, created_at, "
    "records_applied_at, failed_at"
)

# A row whose records are not yet applied: the FIFO and the record drains order against it.
_UNAPPLIED = "(status = 'pending' OR (status = 'failed' AND records_applied_at IS NULL))"

# A row still outstanding in any phase: the run-entry drains and the calls claim order against it.
_OUTSTANDING = "status IN ('pending', 'calls', 'running', 'failed')"


def outbox_row(row: dict[str, Any]) -> OutboxRow:
    """An :class:`OutboxRow` from a fetched ``state_outbox`` row."""
    return OutboxRow(
        id=int(row["id"]),
        status=row["status"],
        record_keys=list(row["record_keys"]),
        subject_keys=list(row["subject_keys"]),
        targets=list(row["targets"]),
        states=list(row["states"]),
        run_id=row["run_id"],
        trace_id=row["trace_id"],
        records=[OutboxItem.model_validate(item) for item in row["records"]],
        subjects=[OutboxSubject.model_validate(item) for item in row["subjects"]],
        calls=[OutboxCall.model_validate(item) for item in row["calls"]],
        calls_done=int(row["calls_done"]),
        attempts=int(row["attempts"]),
        next_attempt_at=row["next_attempt_at"],
        claimed_by=row["claimed_by"],
        lease_until=row["lease_until"],
        last_error=row["last_error"],
        failed_phase=row["failed_phase"],
        created_at=row["created_at"],
        records_applied_at=row["records_applied_at"],
        failed_at=row["failed_at"],
    )


class _OutboxStore(_StoreBase):
    """The ``state_outbox`` statements."""

    # -- enqueue -------------------------------------------------------------
    async def outbox_insert(
        self,
        *,
        record_keys: Sequence[str],
        subject_keys: Sequence[str],
        targets: Sequence[str],
        states: Sequence[str],
        run_id: str | None,
        trace_id: str | None,
        records: Sequence[OutboxItem],
        subjects: Sequence[OutboxSubject],
        calls: Sequence[OutboxCall],
    ) -> int:
        """Insert one row in its own small transaction and return its id.

        A row with records starts ``pending``; a calls-only row starts in ``calls`` with its
        (empty) record part marked applied.
        """
        status_sql = "'pending', NULL" if records else "'calls', clock_timestamp()"
        async with self._write_cursor(None) as cur:
            await cur.execute(
                "INSERT INTO state_outbox (status, records_applied_at, record_keys, subject_keys, targets, states, "  # noqa: S608 constant column list and status predicates, no input
                f"run_id, trace_id, records, subjects, calls) VALUES ({status_sql}, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    list(record_keys),
                    list(subject_keys),
                    list(targets),
                    list(states),
                    run_id,
                    trace_id,
                    Jsonb([item.model_dump(mode="json") for item in records]),
                    Jsonb([item.model_dump(mode="json") for item in subjects]),
                    Jsonb([call.model_dump(mode="json") for call in calls]),
                ),
            )
            row = await cur.fetchone()
            if row is None:
                raise AssertionError("INSERT ... RETURNING returned no row")
            return int(row["id"])

    # -- single-row reads ----------------------------------------------------
    async def outbox_row(self, row_id: int) -> OutboxRow | None:
        """The row ``row_id`` as it stands, or ``None`` when it is gone."""
        async with self._read_cursor(None) as cur:
            await cur.execute(f"SELECT {_COLUMNS} FROM state_outbox WHERE id = %s", (row_id,))  # noqa: S608 constant column list and status predicates, no input
            row = await cur.fetchone()
            return None if row is None else outbox_row(row)

    async def outbox_status(self, row_id: int) -> tuple[str, str | None] | None:
        """``(status, claimed_by)`` of row ``row_id``, or ``None`` when it is gone — one primary-key read."""
        async with self._read_cursor(None) as cur:
            await cur.execute("SELECT status, claimed_by FROM state_outbox WHERE id = %s", (row_id,))
            row = await cur.fetchone()
            return None if row is None else (row["status"], row["claimed_by"])

    async def outbox_record_keys(self, row_id: int) -> list[str] | None:
        """The record keys of row ``row_id`` (read without a lock), or ``None`` when it is gone."""
        async with self._read_cursor(None) as cur:
            await cur.execute("SELECT record_keys FROM state_outbox WHERE id = %s", (row_id,))
            row = await cur.fetchone()
            return None if row is None else list(row["record_keys"])

    # -- the record apply's transaction --------------------------------------
    async def outbox_lock_keys(self, conn: AsyncConnection[Any], keys: Sequence[str], timeout_seconds: float) -> None:
        """Bound every lock wait of the transaction, then take each key's advisory lock in sorted order."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT set_config('lock_timeout', %s, true)", (f"{max(1, int(timeout_seconds * 1000))}ms",)
            )
            for key in sorted(set(keys)):
                await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    async def outbox_older_unapplied(
        self, conn: AsyncConnection[Any], row_id: int, keys: Sequence[str]
    ) -> tuple[int, str] | None:
        """The oldest row older than ``row_id`` with unapplied records on any of ``keys``: ``(id, status)``."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, status FROM state_outbox WHERE id < %s AND record_keys && %s::text[] "  # noqa: S608 constant column list and status predicates, no input
                f"AND {_UNAPPLIED} ORDER BY id LIMIT 1",
                (row_id, list(keys)),
            )
            row = await cur.fetchone()
            return None if row is None else (int(row["id"]), row["status"])

    async def outbox_lock_row(self, conn: AsyncConnection[Any], row_id: int) -> OutboxRow | None:
        """Row ``row_id`` locked ``FOR UPDATE`` on the caller's transaction, or ``None`` when it is gone."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT {_COLUMNS} FROM state_outbox WHERE id = %s FOR UPDATE", (row_id,))  # noqa: S608 constant column list and status predicates, no input
            row = await cur.fetchone()
            return None if row is None else outbox_row(row)

    async def outbox_finish_records(self, conn: AsyncConnection[Any], row_id: int, *, has_calls: bool) -> None:
        """On the apply's transaction: delete the row, or move it to its calls phase."""
        async with conn.cursor(row_factory=dict_row) as cur:
            if has_calls:
                await cur.execute(
                    "UPDATE state_outbox SET status = 'calls', records_applied_at = clock_timestamp(), attempts = 0, "
                    "next_attempt_at = NULL WHERE id = %s",
                    (row_id,),
                )
            else:
                await cur.execute("DELETE FROM state_outbox WHERE id = %s", (row_id,))

    async def outbox_record_failure(
        self,
        row_id: int,
        *,
        phase: str,
        error: str,
        transient: bool,
        max_attempts: int,
        retry_base_seconds: float,
        retry_cap_seconds: float,
        claim: str | None,
    ) -> tuple[str, int] | None:
        """Record one failed attempt; a transient one below ``max_attempts`` backs off, any other fails the row.

        The records phase expects the row ``pending`` and counts the attempt here; the calls phase
        expects it ``running`` under ``claim`` (whose claim already counted the attempt), and a retry
        returns it to ``calls`` with the claim released. Returns the row's ``(status, attempts)``
        after, or ``None`` when the row is not in the expected state.
        """
        retry_status = "pending" if phase == "records" else "calls"
        expect = "status = 'pending'" if phase == "records" else "status = 'running' AND claimed_by = %(claim)s"
        retry = "%(transient)s AND attempts + %(inc)s < %(max)s"
        async with self._write_cursor(None) as cur:
            await cur.execute(
                "UPDATE state_outbox SET attempts = attempts + %(inc)s, last_error = %(error)s, "  # noqa: S608 constant column list and status predicates, no input
                f"status = CASE WHEN {retry} THEN %(retry)s ELSE 'failed' END, "
                f"next_attempt_at = CASE WHEN {retry} THEN clock_timestamp() "
                "+ make_interval(secs => LEAST(%(cap)s, %(base)s * power(2, attempts + %(inc)s - 1))) END, "
                f"failed_phase = CASE WHEN {retry} THEN NULL ELSE %(phase)s END, "
                f"failed_at = CASE WHEN {retry} THEN NULL ELSE clock_timestamp() END, "
                "claimed_by = NULL, lease_until = NULL "
                f"WHERE id = %(id)s AND {expect} RETURNING status, attempts",
                {
                    "inc": 1 if phase == "records" else 0,
                    "error": error,
                    "transient": transient,
                    "max": max_attempts,
                    "retry": retry_status,
                    "cap": retry_cap_seconds,
                    "base": retry_base_seconds,
                    "phase": phase,
                    "id": row_id,
                    "claim": claim,
                },
            )
            row = await cur.fetchone()
            return None if row is None else (row["status"], int(row["attempts"]))

    async def outbox_fail(self, row_id: int, *, phase: str, error: str, claim: str | None) -> bool:
        """Fail row ``row_id`` outright (``running`` under ``claim``); ``False`` when the claim no longer holds."""
        async with self._write_cursor(None) as cur:
            await cur.execute(
                "UPDATE state_outbox SET status = 'failed', failed_phase = %s, failed_at = clock_timestamp(), "
                "last_error = %s, claimed_by = NULL, lease_until = NULL "
                "WHERE id = %s AND status = 'running' AND claimed_by = %s",
                (phase, error, row_id, claim),
            )
            return bool(cur.rowcount)

    # -- the calls claim -----------------------------------------------------
    async def outbox_claim_calls(self, row_id: int, claim: str, lease_seconds: float) -> tuple[str, OutboxRow] | None:
        """Claim row ``row_id``'s calls: ``(previous status, row)``, or ``None`` when another holds them.

        A row in ``calls`` (due), or ``running`` whose lease lapsed, is claimed when no OLDER
        outstanding row shares one of its subject keys. The row is locked and its status read first,
        in the claim's transaction, so the previous status is the one the claim replaces.
        """
        async with self._write_cursor(None) as cur:
            await cur.execute("SELECT status FROM state_outbox WHERE id = %s FOR UPDATE", (row_id,))
            locked = await cur.fetchone()
            if locked is None:
                return None
            await cur.execute(
                "UPDATE state_outbox SET status = 'running', claimed_by = %(claim)s, "  # noqa: S608 constant column list and status predicates, no input
                "lease_until = clock_timestamp() + make_interval(secs => %(lease)s), attempts = attempts + 1 "
                "WHERE id = %(id)s AND ((status = 'calls' AND (next_attempt_at IS NULL OR next_attempt_at <= "
                "clock_timestamp())) OR (status = 'running' AND lease_until < clock_timestamp())) "
                "AND NOT EXISTS (SELECT 1 FROM state_outbox o WHERE o.id < %(id)s "
                f"AND o.subject_keys && state_outbox.subject_keys AND o.{_OUTSTANDING}) "
                f"RETURNING {_COLUMNS}",
                {"id": row_id, "claim": claim, "lease": lease_seconds},
            )
            row = await cur.fetchone()
            return None if row is None else (locked["status"], outbox_row(row))

    async def outbox_heartbeat(self, row_id: int, claim: str, lease_seconds: float) -> bool:
        """Extend the claim's lease; ``False`` when the claim was taken over."""
        async with self._write_cursor(None) as cur:
            await cur.execute(
                "UPDATE state_outbox SET lease_until = clock_timestamp() + make_interval(secs => %s) "
                "WHERE id = %s AND status = 'running' AND claimed_by = %s",
                (lease_seconds, row_id, claim),
            )
            return bool(cur.rowcount)

    async def outbox_call_done(self, row_id: int, claim: str) -> bool:
        """Count one call done under the claim; ``False`` when the claim was taken over."""
        async with self._write_cursor(None) as cur:
            await cur.execute(
                "UPDATE state_outbox SET calls_done = calls_done + 1 WHERE id = %s AND claimed_by = %s",
                (row_id, claim),
            )
            return bool(cur.rowcount)

    async def outbox_delete_claimed(self, row_id: int, claim: str) -> bool:
        """Delete the row after its last call; ``False`` when the claim was taken over."""
        async with self._write_cursor(None) as cur:
            await cur.execute("DELETE FROM state_outbox WHERE id = %s AND claimed_by = %s", (row_id, claim))
            return bool(cur.rowcount)

    # -- the drains ----------------------------------------------------------
    async def outbox_unapplied_on_records(self, keys: Sequence[str]) -> list[tuple[int, str]]:
        """``(id, status)`` of every row with unapplied records on any of ``keys``, in id order."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                f"SELECT id, status FROM state_outbox WHERE record_keys && %s::text[] AND {_UNAPPLIED} ORDER BY id",  # noqa: S608 constant column list and status predicates, no input
                (list(keys),),
            )
            return [(int(r["id"]), r["status"]) for r in await cur.fetchall()]

    async def outbox_unapplied_on_state(self, state: str) -> list[OutboxRow]:
        """Every row with unapplied records writing ``state``, in id order."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM state_outbox WHERE states @> ARRAY[%s]::text[] AND {_UNAPPLIED} ORDER BY id",  # noqa: S608 constant column list and status predicates, no input
                (state,),
            )
            return [outbox_row(r) for r in await cur.fetchall()]

    async def outbox_outstanding_on_subjects(self, keys: Sequence[str]) -> list[tuple[int, str, list[str]]]:
        """``(id, status, subject_keys)`` of every outstanding row on any of ``keys``, in id order."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                "SELECT id, status, subject_keys FROM state_outbox WHERE subject_keys && %s::text[] "  # noqa: S608 constant column list and status predicates, no input
                f"AND {_OUTSTANDING} ORDER BY id",
                (list(keys),),
            )
            return [(int(r["id"]), r["status"], list(r["subject_keys"])) for r in await cur.fetchall()]

    async def outbox_outstanding_on_target(self, target: str) -> list[tuple[int, str, list[str]]]:
        """``(id, status, subject_keys)`` of every outstanding row under the target key ``target``, in id order."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                "SELECT id, status, subject_keys FROM state_outbox WHERE targets @> ARRAY[%s]::text[] "  # noqa: S608 constant column list and status predicates, no input
                f"AND {_OUTSTANDING} "
                "ORDER BY id",
                (target,),
            )
            return [(int(r["id"]), r["status"], list(r["subject_keys"])) for r in await cur.fetchall()]

    async def outbox_blocker(self, row_id: int, subject_keys: Sequence[str]) -> tuple[int, str, list[str]] | None:
        """The oldest outstanding row older than ``row_id`` sharing a subject key: what its calls claim waits on."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                "SELECT id, status, subject_keys FROM state_outbox WHERE id < %s AND subject_keys && %s::text[] "  # noqa: S608 constant column list and status predicates, no input
                f"AND {_OUTSTANDING} ORDER BY id LIMIT 1",
                (row_id, list(subject_keys)),
            )
            row = await cur.fetchone()
            return None if row is None else (int(row["id"]), row["status"], list(row["subject_keys"]))

    async def outbox_check_records(self, conn: AsyncConnection[Any], keys: Sequence[str]) -> bool:
        """Whether any row has unapplied records on ``keys`` — on the caller's transaction."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT EXISTS (SELECT 1 FROM state_outbox WHERE record_keys && %s::text[] "  # noqa: S608 constant column list and status predicates, no input
                f"AND {_UNAPPLIED}) AS pending",
                (list(keys),),
            )
            row = await cur.fetchone()
            return bool(row and row["pending"])

    async def outbox_unapplied_states(self) -> list[str]:
        """Every state some row with unapplied records writes."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                f"SELECT DISTINCT unnest(states) AS state FROM state_outbox WHERE {_UNAPPLIED} ORDER BY state"  # noqa: S608 constant column list and status predicates, no input
            )
            return [r["state"] for r in await cur.fetchall()]

    async def outbox_held_on_state(
        self, conn: AsyncConnection[Any], state: str
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        """``(id, subjects JSON)`` of every row with unapplied records writing ``state`` — under the guard's lock."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, subjects FROM state_outbox WHERE states @> ARRAY[%s]::text[] AND {_UNAPPLIED} ORDER BY id",  # noqa: S608 constant column list and status predicates, no input
                (state,),
            )
            return [(int(r["id"]), list(r["subjects"])) for r in await cur.fetchall()]

    # -- the sweep -----------------------------------------------------------
    async def outbox_due_pending(self, limit: int) -> list[int]:
        """Ids of the ``pending`` rows due for an apply, oldest first."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                "SELECT id FROM state_outbox WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at "
                "<= clock_timestamp()) ORDER BY id LIMIT %s",
                (limit,),
            )
            return [int(r["id"]) for r in await cur.fetchall()]

    async def outbox_due_calls(self, limit: int) -> list[int]:
        """Ids of the rows whose calls are due or whose claim lapsed, oldest first."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                "SELECT id FROM state_outbox WHERE (status = 'calls' AND (next_attempt_at IS NULL OR next_attempt_at "
                "<= clock_timestamp())) OR (status = 'running' AND lease_until < clock_timestamp()) "
                "ORDER BY id LIMIT %s",
                (limit,),
            )
            return [int(r["id"]) for r in await cur.fetchall()]

    async def outbox_status_counts(self) -> list[tuple[str, int, datetime]]:
        """``(status, rows, oldest created_at)`` per status."""
        async with self._read_cursor(None) as cur:
            await cur.execute(
                "SELECT status, count(*) AS n, min(created_at) AS oldest FROM state_outbox GROUP BY status"
            )
            return [(r["status"], int(r["n"]), r["oldest"]) for r in await cur.fetchall()]

    # -- the operator doors --------------------------------------------------
    async def outbox_page(self, *, status: str | None, limit: int, before: int | None) -> list[OutboxRow]:
        """Rows newest first by id, keyset before ``before``.

        ``status`` ``failed`` keeps the failed rows only; ``outstanding`` or ``None`` keeps every row,
        since every row in the outbox is a save not yet applied, the failed ones included.
        """
        where = ["TRUE"]
        params: list[Any] = []
        if status == "failed":
            where.append("status = 'failed'")
        if before is not None:
            where.append("id < %s")
            params.append(before)
        params.append(limit)
        async with self._read_cursor(None) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM state_outbox WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT %s",  # noqa: S608 constant column list and status predicates, no input
                tuple(params),
            )
            return [outbox_row(r) for r in await cur.fetchall()]

    async def outbox_requeue_failed(self, row_id: int) -> OutboxRow | None:
        """Requeue a ``failed`` row: back to ``pending`` (records unapplied) or ``calls``; ``None`` when not failed."""
        async with self._write_cursor(None) as cur:
            await cur.execute(
                "UPDATE state_outbox SET status = "  # noqa: S608 constant column list and status predicates, no input
                "CASE WHEN records_applied_at IS NULL THEN 'pending' ELSE 'calls' END, "
                "attempts = 0, next_attempt_at = NULL, last_error = NULL, failed_phase = NULL, failed_at = NULL "
                f"WHERE id = %s AND status = 'failed' RETURNING {_COLUMNS}",
                (row_id,),
            )
            row = await cur.fetchone()
            return None if row is None else outbox_row(row)

    async def outbox_delete_failed(self, row_id: int) -> OutboxRow | None:
        """Delete a ``failed`` row and return it; ``None`` when it is not failed."""
        async with self._write_cursor(None) as cur:
            await cur.execute(
                f"DELETE FROM state_outbox WHERE id = %s AND status = 'failed' RETURNING {_COLUMNS}",  # noqa: S608 constant column list and status predicates, no input
                (row_id,),
            )
            row = await cur.fetchone()
            return None if row is None else outbox_row(row)
