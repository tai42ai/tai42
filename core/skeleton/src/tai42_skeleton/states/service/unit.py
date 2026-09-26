"""The unit of work over the states facet — staging, projection-served reads, one-transaction commit.

A caller opens a unit for a scope it owns and stages write sets against it. Each staged batch is
validated and projected with the SAME applier the persisted write path runs (the composing-shape
refusal, the guard partition, the ``_trace`` stamp, the pure op engine, the whole-document schema),
but lands in the unit's in-memory staging rather than the store. While the unit is the ambient unit
of the caller's scope, every facet read of a staged subject is served from the projection — the
committed document overlaid with the unit's staged deltas, on a monotonic provisional sequence — so
the scope reads its own staged writes while every other scope still sees the store's committed
document.

``commit`` replays every staged batch through :meth:`~tai42_skeleton.states.service.StatesService.
apply_batch` — ONE store transaction, whole-batch rollback, the ledger and guards authoritative —
and reports any divergence between the staged projection and the committed answer. ``discard`` drops
the staging. A unit neither committed nor discarded when its scope ends is discarded at teardown and
the discard is logged. A savepoint nests: writes staged inside it are kept on a clean exit and
dropped on an exception, only the child's deltas rolling back.
"""

from __future__ import annotations

import logging
import time
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from tai42_contract.states.errors import InvalidPathError
from tai42_contract.states.models import ApplyResult, StateSubject, UnitCommitResult, UnitDivergence

from tai42_skeleton.states.paths import apply_ops as apply_path_ops
from tai42_skeleton.states.paths import partition_guarded, validate_op
from tai42_skeleton.states.schema import _validate_document
from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.store.trace import _iso_now, _refuse_composing_shape, stamp_trace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tai42_contract.states.models import StateBatchWrite

logger = logging.getLogger(__name__)

# The smallest gap the provisional sequence advances by, so two staged writes are strictly
# ordered even inside one wall-clock tick — enough for a reader ordering staged writes.
_SEQ_EPSILON = 1e-6

_SubjectKey = tuple[str, str, str, str, str]
_ApplyContext = tuple[dict[str, Any], list[str], list[tuple[list[Any], str, str]], tuple[tuple[str | int, ...], ...]]

# The ambient unit of work bound to the caller's scope. Homed here (not the kit) because only the
# skeleton's own facet reads consult it; a door outside any unit reads ``None`` and behaves as today.
_current_state_unit: ContextVar[_StateUnit | None] = ContextVar("tai42_states_unit", default=None)


def current_state_unit() -> _StateUnit | None:
    """The unit of work bound to the caller's scope, or ``None`` outside one."""
    return _current_state_unit.get()


class _StateUnit:
    """The staging + projection state of one open unit of work.

    Holds the ordered staged writes (replayed verbatim at commit), the per-subject projected
    document and its provisional sequence, the base committed views read once per touched subject,
    and the ``op_id`` set that makes a staged replay answer ``applied=False``. Not thread-safe: one
    unit belongs to one scope, driven from one task.
    """

    def __init__(self, service: _StatesServiceBase) -> None:
        self._service = service
        self._staged: list[StateBatchWrite] = []
        self._provisional: list[ApplyResult] = []
        self._staged_op_ids: set[str] = set()
        self._base: dict[_SubjectKey, dict[str, Any] | None] = {}
        self._contexts: dict[str, _ApplyContext] = {}
        self._projected: dict[_SubjectKey, dict[str, Any]] = {}
        self._proj_seq: dict[_SubjectKey, float] = {}
        self._last_seq = 0.0
        self._closed = False

    # -- lifecycle guards ----------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("this unit of work is already committed or discarded")

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
        self._ensure_open()
        out: list[ApplyResult] = []
        for item in writes:
            result = await self._stage_one(item)
            self._staged.append(item)
            self._provisional.append(result)
            out.append(result)
        return out

    async def _apply_context(self, state: str) -> _ApplyContext:
        ctx = self._contexts.get(state)
        if ctx is None:
            ctx = await self._service._store.read_apply_context(state)
            self._contexts[state] = ctx
        return ctx

    async def _base_view(self, state: str, subject: StateSubject) -> dict[str, Any] | None:
        key = self._key(state, subject)
        if key not in self._base:
            # Direct store read — the unit's own base is the store's committed document, never its
            # own projection (which is what the reader chokepoint would serve).
            self._base[key] = await self._service._store.read_record_view(state, subject)
        return self._base[key]

    async def _stage_one(self, item: StateBatchWrite) -> ApplyResult:
        effective_schema, subject_kinds, regime_paths, traced_paths = await self._apply_context(item.state)
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
        return await self._project(item, ops, effective_schema, regime_paths, traced_paths)

    async def _project(
        self,
        item: StateBatchWrite,
        ops: list[dict[str, Any]],
        effective_schema: dict[str, Any],
        regime_paths: list[tuple[list[Any], str, str]],
        traced_paths: tuple[tuple[str | int, ...], ...],
    ) -> ApplyResult:
        if not ops:
            return ApplyResult(applied=False, data=None, seq=None, skipped=[])
        op_id = item.op_id
        if op_id is not None and (op_id in self._staged_op_ids or await self._service._store.op_applied(op_id)):
            # A staged replay: no re-write, no projection change — the ledger answers the same at commit.
            return ApplyResult(applied=False, data=None, seq=None, skipped=[])
        key = self._key(item.state, item.subject)
        base = await self._base_view(item.state, item.subject)
        base_seq = base["seq"] if base is not None else 0.0
        current = self._projected.get(key)
        if current is None:
            current = base["data"] if base is not None else {}
        # The composing-shape refusal, guard partition, trace stamp, pure op engine and whole-document
        # schema — the same applier the persisted write path runs, so the projection matches the commit.
        _refuse_composing_shape(ops, regime_paths)
        applied_ops, guard_skipped = partition_guarded(current, ops)
        skipped = [{"op": op.get("op"), "path": op.get("path"), "reason": "guard"} for op in guard_skipped]
        if op_id is not None:
            self._staged_op_ids.add(op_id)
        if not applied_ops:
            if base is not None or key in self._projected:
                return ApplyResult(applied=True, data=current, seq=self._proj_seq.get(key, base_seq), skipped=skipped)
            return ApplyResult(applied=True, data=None, seq=None, skipped=skipped)
        if traced_paths:
            completed = self._service._complete_origin(item.origin)
            stamp = {
                "meta": completed.meta,
                "run": completed.run_id,
                "turn": completed.turn_id,
                "inbound": completed.inbound_id,
                "at": _iso_now(),
            }
            stamp_trace(applied_ops, traced_paths, stamp)
        merged = apply_path_ops(current, applied_ops)
        _validate_document(effective_schema, merged)
        seq = self._next_seq(base_seq)
        self._projected[key] = merged
        self._proj_seq[key] = seq
        return ApplyResult(applied=True, data=merged, seq=seq, skipped=skipped)

    def _next_seq(self, base_seq: float) -> float:
        candidate = max(time.time(), self._last_seq + _SEQ_EPSILON, base_seq + _SEQ_EPSILON)
        self._last_seq = candidate
        return candidate

    # -- commit / discard ----------------------------------------------------
    async def commit(self) -> UnitCommitResult:
        self._ensure_open()
        staged = list(self._staged)
        provisional = list(self._provisional)
        results = await self._service.apply_batch(staged)
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
        self._provisional.clear()
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
            "provisional": len(self._provisional),
            "projected": dict(self._projected),
            "proj_seq": dict(self._proj_seq),
            "op_ids": set(self._staged_op_ids),
            "last_seq": self._last_seq,
        }

    def _restore(self, snapshot: dict[str, Any]) -> None:
        del self._staged[snapshot["staged"] :]
        del self._provisional[snapshot["provisional"] :]
        self._projected = snapshot["projected"]
        self._proj_seq = snapshot["proj_seq"]
        self._staged_op_ids = snapshot["op_ids"]
        self._last_seq = snapshot["last_seq"]


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


class _UnitMixin(_StatesServiceBase):
    """The facet's unit-of-work seam: open a unit, and serve every record read from a bound unit."""

    def open_unit(self) -> AbstractAsyncContextManager[_StateUnit]:
        self._ensure_available()
        return _open_unit(self)

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
