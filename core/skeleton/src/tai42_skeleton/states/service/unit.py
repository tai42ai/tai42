"""The unit of work over the states facet — staging, projection-served reads, one-transaction commit.

A caller opens a unit for a scope it owns and stages write sets against it. Each staged batch is
projected with the SAME applier the persisted write path runs (the composing-shape refusal, the
guard partition, the ``_trace`` stamp, the pure op engine) into the unit's in-memory staging, and
the whole-document schema is checked once per touched subject on the document the batch leaves —
so a batch is accepted or refused as one multi-op ``apply`` would be, and a refused batch leaves
nothing of itself staged. While the unit is the ambient unit of the caller's scope, every facet
read of a staged subject is served from the projection — the committed document overlaid with the
unit's staged deltas, on a monotonic provisional sequence — so the scope reads its own staged
writes while every other scope still sees the store's committed document.

A write door (``replace``/``apply``, and ``merge`` through ``apply``) that runs while the unit is the
ambient unit STAGES into it rather than touching the store, exactly as the read seam serves staged
subjects — so a step's writes read back within the scope and roll back with a discard. A
``conn``-threaded write (the commit itself) always goes to the store.

``commit`` lands every staged subject in ONE store transaction
(:meth:`~tai42_skeleton.states.service.StatesService._commit_writes`): per subject, the already
validated projected document is written under a compare-and-set on the base ``seq`` and the
declaration ``version`` it was staged under; a moved base, a moved version or a ledger conflict
replays that subject's writes through the store's apply path instead, validated once per staged
batch. Any failure rolls the whole commit back, loud; divergence between the staged projection and
the committed answer is reported. ``discard`` drops the staging. A unit neither committed nor
discarded when its scope ends is discarded at teardown and the discard is logged. A savepoint
nests: writes staged inside it are kept on a clean exit and dropped on an exception, only the
child's deltas rolling back.
"""

from __future__ import annotations

import logging
import time
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tai42_contract.states.errors import InvalidPathError
from tai42_contract.states.models import (
    ApplyResult,
    CompletedOrigin,
    StateSubject,
    StateUnitClosedError,
    UnitCommitResult,
    UnitDivergence,
)

from tai42_skeleton.states.paths import apply_ops as apply_path_ops
from tai42_skeleton.states.paths import partition_guarded, validate_op
from tai42_skeleton.states.schema import _validate_document
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.store.trace import _refuse_composing_shape, guard_skip_rows, stamp_trace, trace_stamp
from tai42_skeleton.states.store.writes import ProjectedWrite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from psycopg import AsyncConnection
    from tai42_contract.states.models import StateBatchWrite, WriteOrigin

    from tai42_skeleton.states.store.base import ApplyEntry

logger = logging.getLogger(__name__)

# The smallest gap the provisional sequence advances by, so two staged writes are strictly
# ordered even inside one wall-clock tick — enough for a reader ordering staged writes.
_SEQ_EPSILON = 1e-6

_SubjectKey = tuple[str, str, str, str, str]
_ApplyContext = tuple[int, list[str], "ApplyEntry"]


@dataclass(frozen=True)
class _StagedReplace:
    """A whole-document replace staged in a unit, replayed through the facet's replace door at commit.

    Carried in the unit's ordered staging alongside the ``StateBatchWrite`` ops/template_jq items so a
    replace interleaves with them in authored order; the store is touched only when the unit commits.
    """

    state: str
    subject: StateSubject
    data: dict[str, Any]
    origin: WriteOrigin


@dataclass(frozen=True)
class StagedItemRecord:
    """What one staged write recorded at stage, for the commit.

    ``applied_ops`` (post-guard, ``_trace``-stamped) and ``paths`` are what the write changes;
    ``op_id`` enters the idempotency ledger when ``ledger``; ``row`` records a ``state_writes`` row.
    ``batch`` is the index of the ``stage`` / ``stage_replace`` call that staged it (per unit), and
    ``completed_origin`` the origin completed once at stage. ``version`` and ``base_seq`` are the
    declaration version and the subject's committed ``seq`` (``None`` = no record) it was staged
    against; ``document`` is the subject's projected document after it (``None`` = no projection);
    ``provisional`` its staged answer.
    """

    applied_ops: list[dict[str, Any]]
    paths: list[list[Any]]
    op_id: str | None
    batch: int
    completed_origin: CompletedOrigin
    ledger: bool
    row: bool
    version: int
    base_seq: float | None
    document: dict[str, Any] | None
    provisional: ApplyResult


# The ambient unit of work bound to the caller's scope. Homed here (not the kit) because only the
# skeleton's own facet reads consult it; a door outside any unit reads ``None`` and behaves as today.
_current_state_unit: ContextVar[_StateUnit | None] = ContextVar("tai42_states_unit", default=None)


def current_state_unit() -> _StateUnit | None:
    """The unit of work bound to the caller's scope, or ``None`` outside one."""
    return _current_state_unit.get()


def _subject_where(state: str, subject: StateSubject) -> str:
    return f"state {state!r} subject {subject.target_kind}/{subject.target_name}/{subject.kind}/{subject.key}"


class _StateUnit:
    """The staging + projection state of one open unit of work.

    Holds the ordered staged writes with their stage records, the per-subject projected document
    and its provisional sequence, the base committed views read once per touched subject, the
    per-state apply context (declaration version, subject kinds, entry), and the ``op_id`` set that
    makes a staged replay answer ``applied=False``. Not thread-safe: one unit belongs to one scope,
    driven from one task.
    """

    def __init__(self, service: _StatesServiceBase) -> None:
        self._service = service
        self._staged: list[StateBatchWrite | _StagedReplace] = []
        self._records: list[StagedItemRecord] = []
        self._staged_op_ids: set[str] = set()
        self._base: dict[_SubjectKey, dict[str, Any] | None] = {}
        self._contexts: dict[str, _ApplyContext] = {}
        self._projected: dict[_SubjectKey, dict[str, Any]] = {}
        self._proj_seq: dict[_SubjectKey, float] = {}
        self._last_seq = 0.0
        self._batches = 0
        self._closed = False

    # -- lifecycle guards ----------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise StateUnitClosedError("this unit of work is already committed or discarded")

    @staticmethod
    def _key(state: str, subject: StateSubject) -> _SubjectKey:
        return (state, subject.target_kind, subject.target_name, subject.kind, subject.key)

    # -- projection-served read (consulted by the facet's readers) -----------
    def projected_view(self, state: str, subject: StateSubject) -> dict[str, Any] | None:
        """The projected read view for ``subject``, or ``None`` when the unit has staged nothing for it.

        ``None`` sends the reader to the store's committed document; a value is the committed
        document overlaid with this unit's staged deltas, on the provisional sequence.
        """
        key = self._key(state, subject)
        if key not in self._projected:
            return None
        base = self._base.get(key)
        return {
            "data": self._projected[key],
            "seq": self._proj_seq[key],
            "canonical_subject": base["canonical_subject"] if base is not None else subject,
            "folded_from": base["folded_from"] if base is not None else [],
        }

    # -- staging -------------------------------------------------------------
    async def stage(self, writes: list[StateBatchWrite]) -> list[ApplyResult]:
        """Project ``writes`` as one batch; validate each touched subject's document once; all or nothing."""
        self._ensure_open()
        snapshot = self._snapshot()
        batch = self._next_batch()
        try:
            out: list[ApplyResult] = []
            touched: dict[_SubjectKey, tuple[str, StateSubject]] = {}
            for item in writes:
                record = await self._stage_one(item, batch)
                self._staged.append(item)
                self._records.append(record)
                out.append(record.provisional)
                if record.applied_ops:
                    touched[self._key(item.state, item.subject)] = (item.state, item.subject)
            for key, (state, subject) in touched.items():
                _version, _kinds, entry = await self._apply_context(state)
                _validate_document(entry.validator, self._projected[key], where=_subject_where(state, subject))
        except BaseException:
            self._restore(snapshot)
            raise
        return out

    async def stage_replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], origin: WriteOrigin
    ) -> ApplyResult:
        """Stage a whole-document replace, projecting ``data`` as the subject's document for later reads."""
        self._ensure_open()
        snapshot = self._snapshot()
        batch = self._next_batch()
        item = _StagedReplace(state=state, subject=subject, data=data, origin=origin)
        try:
            record = await self._project_replace(item, batch)
        except BaseException:
            self._restore(snapshot)
            raise
        self._staged.append(item)
        self._records.append(record)
        return record.provisional

    def _next_batch(self) -> int:
        batch = self._batches
        self._batches += 1
        return batch

    async def _apply_context(self, state: str) -> _ApplyContext:
        ctx = self._contexts.get(state)
        if ctx is None:
            ctx = await self._service._store.read_apply_context(state, catalog=self._service._catalog)
            self._contexts[state] = ctx
        return ctx

    async def _base_view(self, state: str, subject: StateSubject) -> dict[str, Any] | None:
        key = self._key(state, subject)
        if key not in self._base:
            # Direct store read — the unit's own base is the store's committed document, never its
            # own projection (which is what the reader chokepoint would serve).
            self._base[key] = await self._service._store.read_record_view(state, subject)
        return self._base[key]

    async def _stage_one(self, item: StateBatchWrite, batch: int) -> StagedItemRecord:
        _version, subject_kinds, _entry = await self._apply_context(item.state)
        await self._service._validate_subject_admitted(subject_kinds, item.state, item.subject)
        if item.ops is not None:
            if not isinstance(item.ops, list):
                raise InvalidPathError("ops must be a list of operations")
            ops = item.ops
        else:
            if item.template_jq is None:
                raise AssertionError
            ops = await self._service._resolve_template_jq_ops(item.state, item.subject, item.template_jq, item.input)
        for i, op in enumerate(ops):
            validate_op(op, where=f"ops[{i}]")
        return await self._project(item, ops, batch)

    async def _project(self, item: StateBatchWrite, ops: list[dict[str, Any]], batch: int) -> StagedItemRecord:
        """Project one staged write onto the subject's document (no schema check: the batch checks it once)."""
        version, _kinds, entry = await self._apply_context(item.state)
        key = self._key(item.state, item.subject)
        base = await self._base_view(item.state, item.subject)
        base_seq = base["seq"] if base is not None else None
        completed = self._service._complete_origin(item.origin)

        def record(result: ApplyResult, *, applied_ops: list[dict[str, Any]], ledger: bool) -> StagedItemRecord:
            return StagedItemRecord(
                applied_ops=applied_ops,
                paths=[list(op.get("path") or []) for op in applied_ops],
                op_id=item.op_id,
                batch=batch,
                completed_origin=completed,
                ledger=ledger,
                row=bool(applied_ops),
                version=version,
                base_seq=base_seq,
                document=self._projected.get(key),
                provisional=result,
            )

        unapplied = ApplyResult(applied=False, data=None, seq=None, skipped=[])
        if not ops:
            return record(unapplied, applied_ops=[], ledger=False)
        op_id = item.op_id
        if op_id is not None and (op_id in self._staged_op_ids or await self._service._store.op_applied(op_id)):
            # A staged replay: no re-write, no projection change — the ledger answers the same at commit.
            return record(unapplied, applied_ops=[], ledger=False)
        current = self._projected.get(key)
        if current is None:
            current = base["data"] if base is not None else {}
        # The composing-shape refusal, guard partition, trace stamp and pure op engine — the same
        # applier the persisted write path runs, so the projection matches the commit.
        _refuse_composing_shape(ops, entry.regime_paths)
        applied_ops, guard_skipped = partition_guarded(current, ops)
        skipped = guard_skip_rows(guard_skipped)
        if op_id is not None:
            self._staged_op_ids.add(op_id)
        ledger = op_id is not None
        if not applied_ops:
            if base is not None or key in self._projected:
                seq = self._proj_seq.get(key, base_seq if base_seq is not None else 0.0)
                return record(
                    ApplyResult(applied=True, data=current, seq=seq, skipped=skipped), applied_ops=[], ledger=ledger
                )
            return record(
                ApplyResult(applied=True, data=None, seq=None, skipped=skipped), applied_ops=[], ledger=ledger
            )
        if entry.traced_paths:
            stamp_trace(applied_ops, entry.traced_paths, trace_stamp(completed))
        merged = apply_path_ops(current, applied_ops)
        seq = self._next_seq(base_seq if base_seq is not None else 0.0)
        self._projected[key] = merged
        self._proj_seq[key] = seq
        return record(
            ApplyResult(applied=True, data=merged, seq=seq, skipped=skipped), applied_ops=applied_ops, ledger=ledger
        )

    async def _project_replace(self, item: _StagedReplace, batch: int) -> StagedItemRecord:
        """Project a staged replace onto the subject's document on a fresh provisional sequence.

        Validates ``data`` whole against the effective schema and mirrors the persisted replace door (no
        composing-shape refusal and no ``_trace`` stamp), so the projection matches what the commit writes.
        """
        version, subject_kinds, entry = await self._apply_context(item.state)
        await self._service._validate_subject_admitted(subject_kinds, item.state, item.subject)
        _validate_document(entry.validator, item.data, where=_subject_where(item.state, item.subject))
        key = self._key(item.state, item.subject)
        base = await self._base_view(item.state, item.subject)
        base_seq = base["seq"] if base is not None else None
        new_doc = deepcopy(item.data)
        seq = self._next_seq(base_seq if base_seq is not None else 0.0)
        self._projected[key] = new_doc
        self._proj_seq[key] = seq
        return StagedItemRecord(
            applied_ops=[],
            paths=[[]],
            op_id=None,
            batch=batch,
            completed_origin=self._service._complete_origin(item.origin),
            ledger=False,
            row=True,
            version=version,
            base_seq=base_seq,
            document=new_doc,
            provisional=ApplyResult(applied=True, data=new_doc, seq=seq, skipped=[]),
        )

    def _next_seq(self, base_seq: float) -> float:
        candidate = max(time.time(), self._last_seq + _SEQ_EPSILON, base_seq + _SEQ_EPSILON)
        self._last_seq = candidate
        return candidate

    # -- commit / discard ----------------------------------------------------
    async def commit(self) -> UnitCommitResult:
        self._ensure_open()
        staged = list(self._staged)
        records = list(self._records)
        provisional = [r.provisional for r in records]
        results = await self._service._commit_writes(staged, staged=records)
        divergences = self._divergences(provisional, results)
        self._closed = True
        self._clear()
        if divergences:
            logger.warning(
                "states unit of work: %d staged write(s) diverged from the projection at commit: %s",
                len(divergences),
                [d.model_dump() for d in divergences],
            )
        return UnitCommitResult(results=results, diverged=bool(divergences), divergences=divergences)

    @staticmethod
    def _divergences(provisional: list[ApplyResult], committed: list[ApplyResult]) -> list[UnitDivergence]:
        out: list[UnitDivergence] = []
        for i, (staged, landed) in enumerate(zip(provisional, committed, strict=True)):
            if staged.applied != landed.applied:
                out.append(UnitDivergence(index=i, field="applied", staged=staged.applied, committed=landed.applied))
            if staged.skipped != landed.skipped:
                out.append(UnitDivergence(index=i, field="skipped", staged=staged.skipped, committed=landed.skipped))
        return out

    async def discard(self) -> None:
        self._ensure_open()
        self._closed = True
        self._clear()

    async def _teardown_discard(self, *, exception: bool) -> None:
        if self._closed:
            return
        self._closed = True
        logger.warning(
            "states unit of work discarded at scope teardown with no explicit commit or discard (%s); "
            "%d staged write(s) dropped",
            "an exception was in flight" if exception else "clean exit",
            len(self._staged),
        )
        self._clear()

    def _clear(self) -> None:
        self._staged.clear()
        self._records.clear()
        self._staged_op_ids.clear()
        self._projected.clear()
        self._proj_seq.clear()

    # -- savepoint -----------------------------------------------------------
    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        self._ensure_open()
        snapshot = self._snapshot()
        try:
            yield
        except BaseException:
            self._restore(snapshot)
            raise

    def _snapshot(self) -> dict[str, Any]:
        return {
            "staged": len(self._staged),
            "records": len(self._records),
            "projected": dict(self._projected),
            "proj_seq": dict(self._proj_seq),
            "op_ids": set(self._staged_op_ids),
            "last_seq": self._last_seq,
            "base": dict(self._base),
            "contexts": dict(self._contexts),
        }

    def _restore(self, snapshot: dict[str, Any]) -> None:
        del self._staged[snapshot["staged"] :]
        del self._records[snapshot["records"] :]
        self._projected = snapshot["projected"]
        self._proj_seq = snapshot["proj_seq"]
        self._staged_op_ids = snapshot["op_ids"]
        self._last_seq = snapshot["last_seq"]
        self._base = snapshot["base"]
        self._contexts = snapshot["contexts"]


@asynccontextmanager
async def _open_unit(service: _StatesServiceBase) -> AsyncIterator[_StateUnit]:
    """Bind a fresh unit as the ambient unit for the block, discarding it at teardown if unresolved."""
    unit = _StateUnit(service)
    token = _current_state_unit.set(unit)
    try:
        try:
            yield unit
        except BaseException:
            await unit._teardown_discard(exception=True)
            raise
        else:
            await unit._teardown_discard(exception=False)
    finally:
        _current_state_unit.reset(token)


def replace_result(result: ApplyResult, seq: float | None) -> ApplyResult:
    """``result`` with the committed ``seq``."""
    return result.model_copy(update={"seq": seq})


class _UnitMixin(_StatesServiceBase):
    """The facet's unit-of-work seam: open a unit, and serve every record read from a bound unit."""

    def open_unit(self) -> AbstractAsyncContextManager[_StateUnit]:
        self._ensure_available()
        return _open_unit(self)

    async def _commit_writes(
        self,
        writes: Sequence[StateBatchWrite | _StagedReplace],
        *,
        staged: Sequence[StagedItemRecord] | None = None,
        conn: AsyncConnection[Any] | None = None,
    ) -> list[ApplyResult]:
        """Apply an ordered write set — ops/template_jq batch items and whole-document replaces — as ONE transaction.

        One :class:`ApplyResult` per item in input order. Items are STABLY sorted by ``(state, subject)``
        so two concurrent multi-subject commits take the per-declaration locks in one order rather than
        opposing; items sharing a ``(state, subject)`` keep their input order, so a subject's writes land
        as authored. Any item that raises propagates out of the transaction, rolling the WHOLE batch back
        — no partial write survives, loud. An empty set returns ``[]`` without opening a transaction.

        Without ``staged`` (``apply_batch``) each item is dispatched on the shared connection — a replace
        through :meth:`replace`, an ``ops`` item through :meth:`apply`, a ``template_jq`` item through
        :meth:`apply_template_jq` — so items on one subject read each other's uncommitted writes in
        order. With ``staged`` (a unit's commit, one record per item) each subject's validated projected
        document is written under a compare-and-set; a subject whose base, declaration version or
        ledger moved replays its items through the store's apply path, validated once per staged batch.
        With ``conn`` everything runs on the caller's transaction (no ``begin``, no commit of its own).
        """
        if not writes:
            return []
        order = sorted(range(len(writes)), key=lambda i: self._subject_sort_key(writes[i]))
        if conn is not None:
            return await self._commit_on(conn, writes, order, staged)
        async with self._store.begin() as own:
            return await self._commit_on(own, writes, order, staged)

    @staticmethod
    def _subject_sort_key(item: StateBatchWrite | _StagedReplace) -> tuple[str, str, str, str, str]:
        return (item.state, item.subject.target_kind, item.subject.target_name, item.subject.kind, item.subject.key)

    async def _commit_on(
        self,
        conn: AsyncConnection[Any],
        writes: Sequence[StateBatchWrite | _StagedReplace],
        order: list[int],
        staged: Sequence[StagedItemRecord] | None,
    ) -> list[ApplyResult]:
        results: dict[int, ApplyResult] = {}
        if staged is None:
            for i in order:
                results[i] = await self._dispatch(writes[i], conn)
            return [results[i] for i in range(len(writes))]
        groups: dict[tuple[str, str, str, str, str], list[int]] = {}
        for i in order:
            groups.setdefault(self._subject_sort_key(writes[i]), []).append(i)
        for indices in groups.values():
            first = writes[indices[0]]
            records = [staged[i] for i in indices]
            landed, seq = await self._store.write_projected(
                first.state,
                first.subject,
                version=records[0].version,
                base_seq=records[0].base_seq,
                document=records[-1].document,
                writes=[
                    ProjectedWrite(op_id=r.op_id, paths=r.paths, origin=r.completed_origin, ledger=r.ledger, row=r.row)
                    for r in records
                ],
                conn=conn,
            )
            if landed:
                for i in indices:
                    provisional = staged[i].provisional
                    results[i] = provisional if provisional.seq is None else replace_result(provisional, seq)
                continue
            replayed = await self._replay_subject(conn, writes, staged, indices)
            results.update(zip(indices, replayed, strict=True))
        return [results[i] for i in range(len(writes))]

    async def _dispatch(self, item: StateBatchWrite | _StagedReplace, conn: AsyncConnection[Any]) -> ApplyResult:
        """Apply one item on ``conn`` through its facet door (validated per item)."""
        if isinstance(item, _StagedReplace):
            record = await self.replace(item.state, item.subject, item.data, origin=item.origin, conn=conn)
            return ApplyResult(applied=True, data=record.data, seq=record.seq, skipped=[])
        if item.ops is not None:
            return await self.apply(item.state, item.subject, item.ops, op_id=item.op_id, origin=item.origin, conn=conn)
        if item.template_jq is None:
            raise AssertionError
        outcome = await self.apply_template_jq(
            item.state, item.subject, item.template_jq, item.input, op_id=item.op_id, origin=item.origin, conn=conn
        )
        return ApplyResult(applied=outcome.applied, data=outcome.data, seq=outcome.seq, skipped=outcome.skipped)

    async def _replay_subject(
        self,
        conn: AsyncConnection[Any],
        writes: Sequence[StateBatchWrite | _StagedReplace],
        staged: Sequence[StagedItemRecord],
        indices: list[int],
    ) -> list[ApplyResult]:
        """Replay one subject's staged items through the apply path, validating once per staged batch.

        Each item runs unvalidated under its stage-time completed origin; after the last item of each
        batch the subject's document as it now stands is validated with the entry of the declaration
        version the replay holds locked, so the commit refuses only what one multi-op apply would.
        """
        last_of_batch = {staged[i].batch: i for i in indices}
        out: list[ApplyResult] = []
        for i in indices:
            item = writes[i]
            origin = staged[i].completed_origin
            if isinstance(item, _StagedReplace):
                record = await self._replace_completed(
                    item.state, item.subject, item.data, origin=origin, conn=conn, validate=False
                )
                result = ApplyResult(applied=True, data=record.data, seq=record.seq, skipped=[])
            elif item.ops is not None:
                result = await self._apply_completed(
                    item.state, item.subject, item.ops, op_id=item.op_id, origin=origin, conn=conn, validate=False
                )
            else:
                if item.template_jq is None:
                    raise AssertionError
                outcome = await self._apply_template_jq_completed(
                    item.state,
                    item.subject,
                    item.template_jq,
                    item.input,
                    op_id=item.op_id,
                    origin=origin,
                    conn=conn,
                    validate=False,
                )
                result = ApplyResult(
                    applied=outcome.applied, data=outcome.data, seq=outcome.seq, skipped=outcome.skipped
                )
            out.append(result)
            if last_of_batch[staged[i].batch] == i:
                await self._validate_replayed(conn, item.state, item.subject, result)
        return out

    async def _validate_replayed(
        self, conn: AsyncConnection[Any], state: str, subject: StateSubject, result: ApplyResult
    ) -> None:
        document = result.data
        if document is None:
            view = await self._store.read_record_view(state, subject, conn=conn)
            if view is None:
                return
            document = view["data"]
        entry = await self._store.locked_entry(state, catalog=self._catalog, conn=conn)
        _validate_document(entry.validator, document, where=_subject_where(state, subject))

    async def _projected_record_view(
        self, state: str, subject: StateSubject, *, conn: Any | None = None
    ) -> dict[str, Any] | None:
        """The record view a facet reader gets: the bound unit's projection, else the store's committed read.

        The ONE seam :meth:`read`, :meth:`eval_template_jq` and the update program's server-side
        record read flow through, so every facet reader honours a unit at one place. A read that
        threads a ``conn`` (an attach reconciler in its own transaction, or the commit replaying
        through the write transaction) reads the store directly — a unit is an out-of-transaction
        projection, never consulted on a transaction-bound read.
        """
        if conn is None:
            unit = current_state_unit()
            if unit is not None:
                view = unit.projected_view(state, subject)
                if view is not None:
                    return view
        return await self._store.read_record_view(state, subject, conn=conn)
