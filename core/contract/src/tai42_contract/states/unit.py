"""The states facet's unit of work: a scope's writes staged and projected, committed once, or discarded."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from tai42_contract.states.models import ApplyResult, StateBatchWrite, UnitCommitResult


class StateUnitClosedError(RuntimeError):
    """The unit of work was already committed or discarded; it accepts no further staging, savepoint or commit."""


@runtime_checkable
class StateUnit(Protocol):
    """A unit of work over the states facet: a scope's writes staged and projected, committed once, or discarded.

    A caller opens a unit for a scope it owns and stages write sets against it. Each staged
    batch is validated and projected exactly the way a committed apply would be — guards, the
    path ops, the composing-shape rule and the whole-document schema, the SAME applier — but
    lands in the unit's staging, not the store. While the unit is bound to the caller's scope,
    every facet read of a subject the unit has staged is served from the projection (the
    committed document overlaid with the unit's staged deltas in order, on a monotonic
    provisional sequence), so the scope reads its own staged writes while every other scope
    still sees the store's committed document. ``defer_call`` stages a tool call to run after the
    unit's writes apply. ``commit`` writes the staged batches and calls durably as one pending
    save, applied after the caller returns in per-subject order. ``discard`` drops the staging.
    A unit neither committed nor discarded by the end of its scope is discarded by the scope
    teardown, an error the caller logs.
    """

    async def stage(self, writes: list[StateBatchWrite]) -> list[ApplyResult]:
        """Validate and project ``writes`` into the unit's staging, returning the provisional per-write result.

        Each write is validated and projected exactly as
        :meth:`~tai42_contract.app.facets.AppStates.apply` /
        :meth:`~tai42_contract.app.facets.AppStates.apply_template_jq` would apply it, over the
        unit's current projection, and returns its provisional :class:`ApplyResult` (a re-staged
        ``op_id`` — already staged here or already committed — answers ``applied=False`` without
        touching the projection, mirroring the ledger at commit). The whole-document schema is
        checked once per touched subject, on the document the batch's last write for it leaves, so
        a batch is accepted or refused as one multi-op ``apply`` would be. No store write happens;
        a validation, composing-shape or schema error raises loudly and leaves nothing of the batch
        staged. A committed or discarded unit raises :class:`StateUnitClosedError`.
        """
        ...

    async def commit(self) -> UnitCommitResult:
        """Write every staged batch and deferred call durably as ONE pending save in one store transaction.

        Returns the provisional results; the save is applied after the caller returns, in
        per-subject order, and every later reader of its subjects waits for it. Validation already
        ran at stage. A committed or discarded unit raises :class:`StateUnitClosedError`.
        """
        ...

    async def defer_call(self, tool: str, arguments: dict[str, Any], *, run_id: str | None = None) -> None:
        """Stage a call of ``tool`` with ``arguments`` to run after the unit's pending save applies.

        Captured with the caller's execution identity, state context and run attribution; dropped
        with the unit on discard and with a savepoint on its rollback. ``run_id`` names the
        caller's run, as ``WriteOrigin.run_id`` does. Raises
        :class:`~tai42_contract.states.errors.DeferredCallRefusedError` when the tool is not
        registered or the arguments are not plain JSON.
        """
        ...

    async def discard(self) -> None:
        """Drop the unit's staging without writing.

        A committed or discarded unit raises :class:`StateUnitClosedError`.
        """
        ...

    def savepoint(self) -> AbstractAsyncContextManager[None]:
        """A nested staging scope inside the unit.

        Writes staged inside the ``async with`` block are KEPT on a clean exit and DROPPED on an
        exception — only the child's staged deltas roll back, and the exception propagates
        loudly. Savepoints nest to any depth. Entering one on a committed or discarded unit raises
        :class:`StateUnitClosedError`.
        """
        ...
