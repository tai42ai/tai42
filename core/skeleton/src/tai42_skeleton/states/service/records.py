"""The record doors.

Read, replace, merge, apply, erase, fold, listing, search, the write audit page, the retention
prune, and the backup restore paths.

Every write completes its origin through the provenance chokepoint and validates the whole
document against the effective schema before it lands.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from psycopg import AsyncConnection
from tai42_contract.states.errors import (
    InvalidPathError,
    StateNotFoundError,
    SubjectFoldError,
    SubjectRefusedError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    MAX_RETENTION_DAYS,
    ApplyResult,
    CompletedOrigin,
    StateBatchWrite,
    StateRecord,
    StateSubject,
    WriteEntry,
    WriteOrigin,
    WritesPage,
)

from tai42_skeleton.states.paths import validate_op
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.rows import _page_limit, _subject_from_row
from tai42_skeleton.states.service.unit import current_state_unit
from tai42_skeleton.states.store import make_cursor, store_settings_retention
from tai42_skeleton.states.store.trace import guard_skip_rows

logger = logging.getLogger(__name__)


class _RecordMixin(_StatesServiceBase):
    async def read(
        self, state: str, subject: StateSubject, *, conn: AsyncConnection[Any] | None = None
    ) -> StateRecord | None:
        """The record for ``subject`` (resolving a fold), or ``None`` when none exists.

        An unknown person or a target mismatch is a refusal, never an empty document. With ``conn``
        the read joins the caller's transaction (a attach reconciler reading its own in-flight
        merges).
        """
        self._ensure_available()
        await self._admit_subject(state, subject)
        view = await self._projected_record_view(state, subject, conn=conn)
        if view is None:
            return None
        return StateRecord(
            state=state,
            subject=subject,
            data=view["data"],
            seq=view["seq"],
            canonical_subject=view["canonical_subject"],
            folded_from=view["folded_from"],
        )

    async def replace(
        self,
        state: str,
        subject: StateSubject,
        data: dict[str, Any],
        *,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> StateRecord:
        """Replace ``subject``'s whole document with ``data`` and return the new record.

        While an out-of-transaction unit of work is bound to the caller's scope the replace
        STAGES into it rather than touching the store, so it reads back within the scope and
        rolls back with a discard — mirroring the read seam. A ``conn``-threaded call (the
        unit's own commit) writes the store directly. The subject is admitted where the write is
        decided: by the unit when it stages, else inside the write transaction under the
        declaration lock.
        """
        self._ensure_available()
        if not isinstance(data, dict):
            raise ValueValidationError("a record document must be a JSON object")
        if conn is None:
            unit = current_state_unit()
            if unit is not None:
                await unit.stage_replace(state, subject, data, origin)
                view = await self.read(state, subject)
                if view is None:
                    raise AssertionError
                return view
        return await self._replace_completed(state, subject, data, origin=self._complete_origin(origin), conn=conn)

    async def _replace_completed(
        self,
        state: str,
        subject: StateSubject,
        data: dict[str, Any],
        *,
        origin: CompletedOrigin,
        conn: AsyncConnection[Any] | None = None,
        validate: bool = True,
    ) -> StateRecord:
        """The store replace under an already-completed origin; ``validate=False`` leaves the check to the caller.

        The subject is admitted inside the write transaction from the locked declaration's kinds, so a
        replay after the state was re-declared is refused by the declaration it would land under.
        """

        async def _validate_subject_in_txn(subject_kinds: list[str]) -> None:
            await self._validate_subject_admitted(subject_kinds, state, subject)

        await self._store.replace(
            state,
            subject,
            data,
            origin=origin,
            catalog=self._catalog,
            validate_subject_in_txn=_validate_subject_in_txn,
            validate=validate,
            conn=conn,
        )
        view = await self._projected_record_view(state, subject, conn=conn)
        if view is None:
            raise AssertionError
        return StateRecord(
            state=state,
            subject=subject,
            data=view["data"],
            seq=view["seq"],
            canonical_subject=view["canonical_subject"],
            folded_from=view["folded_from"],
        )

    async def merge(
        self, state: str, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin
    ) -> StateRecord:
        """Shallow top-level merge ``patch`` into ``subject``'s document and return the new record.

        One ``set`` op per top-level key, applied atomically under the record lock.
        """
        self._ensure_available()
        if not isinstance(patch, dict):
            raise ValueValidationError("a merge patch must be a JSON object")
        ops = [{"op": "set", "path": [k], "value": v} for k, v in patch.items()]
        await self.apply(state, subject, ops, op_id=None, origin=origin)
        view = await self.read(state, subject)
        if view is None:
            # An empty patch touched nothing and no record exists — represent the still-empty
            # document rather than inventing a write.
            return StateRecord(state=state, subject=subject, data={}, seq=0.0, canonical_subject=subject)
        return view

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> ApplyResult:
        """Apply an op batch to ``subject``'s document under the effective schema.

        Refuses a composing-path shape violation before the ledger insert, stamps ``_trace`` under a
        traced attach, and records one write row. A replayed ``op_id`` returns ``applied=False``;
        guarded ops land in ``skipped``. With ``conn`` the write joins the caller's transaction (a
        attach reconciler's resolution).
        """
        self._ensure_available()
        if not isinstance(ops, list):
            raise InvalidPathError("ops must be a list of operations")
        if not ops:
            return ApplyResult(applied=False, data=None, seq=None, skipped=[])
        if conn is None:
            unit = current_state_unit()
            if unit is not None:
                # An out-of-transaction unit is bound to the caller's scope: stage the write into it
                # (read-your-writes within the scope, a discard that truly discards, ordered replay at
                # commit) rather than touching the store, mirroring the read seam. A ``conn``-threaded
                # call (the unit's own commit) falls through to the direct store write below.
                staged = await unit.stage(
                    [StateBatchWrite(state=state, subject=subject, ops=ops, op_id=op_id, origin=origin)]
                )
                return staged[0]
        return await self._apply_completed(
            state, subject, ops, op_id=op_id, origin=self._complete_origin(origin), conn=conn
        )

    async def _apply_completed(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: CompletedOrigin,
        conn: AsyncConnection[Any] | None = None,
        validate: bool = True,
    ) -> ApplyResult:
        """The store apply under an already-completed origin; ``validate=False`` leaves the check to the caller."""
        if not ops:
            return ApplyResult(applied=False, data=None, seq=None, skipped=[])
        for i, op in enumerate(ops):
            validate_op(op, where=f"ops[{i}]")

        async def _validate_subject_in_txn(subject_kinds: list[str]) -> None:
            """Refuse the subject under the declaration lock, from the row read in the write txn."""
            await self._validate_subject_admitted(subject_kinds, state, subject)

        applied, data, seq, skipped = await self._store.apply_ops(
            state,
            subject,
            ops,
            op_id=op_id,
            origin=origin,
            catalog=self._catalog,
            validate_subject_in_txn=_validate_subject_in_txn,
            validate=validate,
            conn=conn,
        )
        return ApplyResult(
            applied=applied,
            data=data,
            seq=seq,
            skipped=guard_skip_rows(skipped),
        )

    async def apply_batch(self, writes: list[StateBatchWrite]) -> list[ApplyResult]:
        """Apply an ordered write set as ONE transaction, one :class:`ApplyResult` per item in input order.

        Opens ``store.begin()`` ONCE and dispatches each item on the shared connection — an ``ops``
        item through :meth:`apply`, a ``template_jq`` item through :meth:`apply_template_jq` (its
        outcome mapped onto the item's :class:`ApplyResult`) — so items on one subject read each
        other's uncommitted writes in the order they are applied. Items are applied STABLY sorted by
        ``(state, subject)`` so two concurrent multi-state batches take the per-declaration locks in
        one order rather than opposing (defensive: a subject write takes FOR SHARE, which never
        conflicts with FOR SHARE, but a concurrent fold/attach holds FOR UPDATE); items sharing a
        ``(state, subject)`` keep their input order, so a subject's writes still land as authored, and
        the returned list is in the caller's input order regardless. Any item that raises propagates
        out of the transaction, rolling the WHOLE batch back — no partial write survives, loud.
        ``op_id`` idempotency holds per item (a replayed key returns ``applied=False`` while its
        siblings land). An empty list returns ``[]`` without opening a transaction; the transaction's
        connection stays hidden behind this seam.
        """
        self._ensure_available()
        if not writes:
            return []
        return await self._commit_writes(writes)

    async def erase(self, state: str, subject: StateSubject, *, origin: WriteOrigin) -> None:
        """Erase ``subject``'s record, recording the write."""
        self._ensure_available()
        await self._admit_subject(state, subject)
        completed = self._complete_origin(origin)
        await self._store.erase_subject(state, subject, origin=completed)

    async def fold(
        self, state: str, subject: StateSubject, into: StateSubject, mode: str, *, origin: WriteOrigin
    ) -> dict[str, Any]:
        """Fold ``subject`` into ``into`` (``switch`` drops, ``merge`` combines; survivor wins), return the report."""
        self._ensure_available()
        if mode not in ("switch", "merge"):
            raise SubjectFoldError(f"unknown fold mode {mode!r} (supported: merge, switch)")
        _version, subject_kinds, _default = await self._admit_subject(state, subject)
        await self._validate_subject_admitted(subject_kinds, state, into)
        completed = self._complete_origin(origin)
        return await self._store.fold_subject(state, subject, into, mode, origin=completed, catalog=self._catalog)

    async def list_subjects(
        self,
        state: str,
        *,
        kind: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        conn: AsyncConnection[Any] | None = None,
    ) -> dict[str, Any]:
        """One keyset page of a state's subjects, ordered by the full subject identity.

        The order is ``(target_kind, target_name, kind, key)``. With ``conn`` the read joins the
        caller's transaction (a attach reconciler paging its own in-flight merges).
        """
        self._ensure_available()
        page = _page_limit(limit)
        if await self._store.declaration_version(state) is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        rows = await self._store.list_subjects(state, kind=kind, limit=page, cursor=cursor, conn=conn)
        next_cursor = (
            make_cursor(
                rows[-1]["target_kind"], rows[-1]["target_name"], rows[-1]["subject_kind"], rows[-1]["subject_key"]
            )
            if len(rows) == page
            else None
        )
        return {"subjects": [_subject_from_row(state, r) for r in rows], "next_cursor": next_cursor}

    async def search(
        self, state: str, filters: dict[str, Any], *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Content search — the subjects whose record data CONTAINS ``filters``.

        ``filters`` is a JSONB containment document, matched with ``data @> filters``. A non-object
        or empty ``filters`` is a loud client error.
        """
        self._ensure_available()
        page = _page_limit(limit)
        if not isinstance(filters, dict) or not filters:
            raise ValueValidationError("search needs a non-empty filters object (a JSONB containment document)")
        if await self._store.declaration_version(state) is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        rows = await self._store.search_records(state, filters, limit=page, cursor=cursor)
        next_cursor = (
            make_cursor(
                rows[-1]["target_kind"], rows[-1]["target_name"], rows[-1]["subject_kind"], rows[-1]["subject_key"]
            )
            if len(rows) == page
            else None
        )
        return {"matches": [_subject_from_row(state, r) for r in rows], "next_cursor": next_cursor}

    async def writes(
        self, state: str, subject: StateSubject, *, limit: int | None = None, cursor: str | None = None
    ) -> WritesPage:
        """One keyset page of ``subject``'s audit trail, newest first.

        The ``items`` (each a write with its completed origin and touched paths) and the
        ``next_cursor`` the next call pages from (the last row's id when the page is full, else
        ``None``).
        """
        self._ensure_available()
        page = _page_limit(limit)
        if cursor is not None:
            try:
                int(cursor)
            except (TypeError, ValueError):
                raise ValueValidationError(f"writes cursor must be a row id (an integer), got {cursor!r}") from None
        rows = await self._store.writes(state, subject, limit=page, cursor=cursor)
        items = [
            WriteEntry(
                seq=row["seq"] if row["seq"] is not None else 0.0,
                at=row["at"],
                origin=CompletedOrigin(
                    consumer=row["consumer"],
                    meta=row["meta"],
                    run_id=row["run_id"],
                    op_id=row["op_id"],
                    door=row["door"],
                    actor=row["actor"],
                    turn_id=row["turn_id"],
                ),
                paths=[list(p) for p in (row["paths"] or [])],
            )
            for row in rows
        ]
        next_cursor = str(rows[-1]["id"]) if len(rows) == page else None
        return WritesPage(items=items, next_cursor=next_cursor)

    async def prune_expired(self) -> dict[str, int]:
        """The explicit retention sweep — delete every record past its state's effective retention.

        Also prunes the op-idempotency ledger past its own retention window: this externally
        scheduled sweep is the ledger's only prune, so a deployment that never runs it keeps a
        growing ``state_applied_ops`` exactly as it keeps un-pruned records. A misconfigured
        global default is refused loudly before any delete; a failing prune raises.

        Returns the per-state record removal counts.
        """
        self._ensure_available()
        from tai42_skeleton.states import service as _pkg

        default = _pkg.store_settings_default_retention()
        if default is not None and (isinstance(default, bool) or default < 1 or default > MAX_RETENTION_DAYS):
            raise ValueValidationError(
                f"STATES_DEFAULT_RETENTION_DAYS must be a positive integer ≤ {MAX_RETENTION_DAYS} or unset, "
                f"got {default!r}"
            )
        counts = await self._store.prune_expired(default)
        op_pruned = await self._store.prune_ops(store_settings_retention())
        if counts or op_pruned:
            logger.info(
                "states retention prune: deleted %d record(s) across %d state(s) and %d op-ledger row(s)",
                sum(counts.values()),
                len(counts),
                op_pruned,
            )
        return counts

    async def restore_records(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """Restore record rows for ``state`` under the completed origin, validating each document against the schema.

        The backup section's own record-restore path (off the ``AppStates`` protocol).

        EVERY row's subject is validated (declared kind, non-empty key, and — for kind
        ``person`` — a known person of the row's target) through the declaration's admission check
        BEFORE any write; a refusal names the offending row index and its subject and
        nothing is written, so a restore never lands records under an undeclared kind or an
        unknown person.
        """
        self._ensure_available()
        from pydantic import ValidationError

        scalars = await self._store.declaration_scalars(state)
        if scalars is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        subject_kinds = scalars[1]
        row_list = list(rows)
        for index, row in enumerate(row_list):
            try:
                subject = StateSubject(
                    target_kind=row["target_kind"],
                    target_name=row["target_name"],
                    kind=row["subject_kind"],
                    key=row["subject_key"],
                )
            except ValidationError as exc:
                raise SubjectRefusedError(
                    f"restore row {index}: malformed subject "
                    f"{row.get('target_kind')!r}/{row.get('target_name')!r}/"
                    f"{row.get('subject_kind')!r}/{row.get('subject_key')!r}: {exc}"
                ) from exc
            try:
                await self._validate_subject_admitted(subject_kinds, state, subject)
            except SubjectRefusedError as exc:
                raise SubjectRefusedError(f"restore row {index}: {exc}") from exc
        completed = self._complete_origin(origin)
        await self._store.restore_records(state, row_list, origin=completed, catalog=self._catalog)

    async def restore_aliases(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """Restore subject-alias rows for ``state`` verbatim (identity, not a write).

        The backup section's restore path, off the ``AppStates`` protocol.
        """
        self._ensure_available()
        await self._store.restore_aliases(state, list(rows))
