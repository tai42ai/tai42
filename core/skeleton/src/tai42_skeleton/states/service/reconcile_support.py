"""The narrow record door an attach reconciler works through, and the reconcile refusal shapes.

A reconciler reads and writes a state's records on the attach transaction through
:class:`_AttachReconcileRecords`; the refusal helpers build the message and structured
payload the platform's own template reconciler raises when a declarations edit would orphan
open records.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_contract.states.errors import ValueValidationError
from tai42_contract.states.models import (
    ApplyResult,
    AttachReconcileContext,
    StateRecord,
    StateSubject,
    WriteOrigin,
)

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from .base import _StatesServiceBase

# One record page and the orphan-listing cap keep the reconcile refusal message and its read
# loop bounded on a state with many subjects.
_RECONCILE_PAGE = 200
_RECONCILE_LIST_CAP = 20
# Every reconcile close write carries this generic origin — a consumer name, never a template.
_RECONCILE_ORIGIN = WriteOrigin(consumer="attach-reconcile", meta={"origin": "reconcile"})


class _AttachReconcileRecords:
    """The narrow record door an attach reconciler reads and writes through.

    The :class:`~tai42_contract.states.AttachReconcileRecords` handle bound to one
    state and the attach's transaction ``conn``. Every call runs on that
    transaction: ``merge`` and the keyed ``apply`` write on it (so a reconciler's
    resolution commits with the attach or rolls back with a refusal), and
    ``read``/``list_subjects`` read on it too, so a reconciler sees its own
    in-flight merges. Writes are completed and audited through the service
    chokepoint exactly like any facet write.
    """

    def __init__(self, service: _StatesServiceBase, state: str, conn: AsyncConnection[Any]) -> None:
        self._service = service
        self._state = state
        self._conn = conn

    async def read(self, subject: StateSubject) -> StateRecord | None:
        return await self._service.read(self._state, subject, conn=self._conn)

    async def list_subjects(
        self, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._service.list_subjects(self._state, kind=kind, limit=limit, cursor=cursor, conn=self._conn)

    async def merge(self, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin) -> StateRecord:
        """Shallow top-level merge ``patch`` into ``subject`` on the attach transaction."""
        if not isinstance(patch, dict):
            raise ValueValidationError("a merge patch must be a JSON object")
        ops = [{"op": "set", "path": [k], "value": v} for k, v in patch.items()]
        result = await self._service.apply(self._state, subject, ops, op_id=None, origin=origin, conn=self._conn)
        data = result.data if result.data is not None else {}
        seq = result.seq if result.seq is not None else 0.0
        return StateRecord(state=self._state, subject=subject, data=data, seq=seq, canonical_subject=subject)

    async def apply(self, subject: StateSubject, ops: list[dict[str, Any]], *, origin: WriteOrigin) -> ApplyResult:
        """Apply an op batch (the same keyed ops as an update-purpose program) to ``subject`` on the attach transaction.

        So a record under a ``composing`` write regime can be closed with a keyed
        op that ``merge``'s whole-path set would refuse.
        """
        return await self._service.apply(self._state, subject, ops, op_id=None, origin=origin, conn=self._conn)


def _record_subtree(data: dict[str, Any], path: list[str]) -> dict[str, Any]:
    """The record document at an attachment's ``path`` — the subtree a template's jq programs operate over.

    ``.`` at the seam. An absent or non-object node reads as ``{}``.
    """
    node: Any = data
    for seg in path:
        if not isinstance(node, dict):
            return {}
        node = node.get(seg)
    return node if isinstance(node, dict) else {}


def _rebase_op(op: Any, path: list[str]) -> dict[str, Any]:
    """One template-relative op with its ``path`` rebased under the attachment ``path``.

    So an update program (like a fill) authors its ops in its own coordinates. A
    malformed op is a loud refusal.
    """
    if not isinstance(op, dict) or not isinstance(op.get("path"), list):
        raise ValueValidationError(f"an update-program op must be an object carrying a list path, got {op!r}")
    return {**op, "path": [*path, *op["path"]]}


def _reconcile_refusal(context: AttachReconcileContext, orphans: list[tuple[StateSubject, dict[str, Any]]]) -> str:
    shown = orphans[:_RECONCILE_LIST_CAP]
    listed = "; ".join(
        f"[{subject.kind}:{subject.key}] {item.get('label', item.get('id'))} (id {item.get('id')})"
        for subject, item in shown
    )
    more = len(orphans) - len(shown)
    if more > 0:
        listed = f"{listed}; … and {more} more"
    return (
        f"re-attaching template {context.template.name!r} on state {context.state!r} would orphan "
        f"{len(orphans)} open record item(s) the new declarations no longer cover: {listed}. "
        'Re-attach with options {"orphans": "close", "resolution": "<not-done resolution>"} to close them.'
    )


def _reconcile_orphans_extra(orphans: list[tuple[StateSubject, dict[str, Any]]]) -> dict[str, Any]:
    """The reconcile refusal's STRUCTURED payload of the orphaned records.

    Carries each orphan's subject key/kind and the orphan item's id/label so a UI
    keys its resolve step on the data, not the prose. ``reconcile`` flags the one
    refusal that has the close-with-resolution follow-up.
    """
    return {
        "reconcile": True,
        "orphans": [
            {
                "subject": subject.key,
                "kind": subject.kind,
                "id": item.get("id"),
                "label": item.get("label", item.get("id")),
            }
            for subject, item in orphans
        ],
    }
