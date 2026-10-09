"""What a unit stages, and the conversion to and from the pending-save row that carries it.

A unit's staging is an ordered list of writes (a :class:`~tai42_contract.states.StateBatchWrite`
or a :class:`StagedReplace`), each with the :class:`StagedItemRecord` it recorded at stage. The
commit serializes both into one outbox row; the applier rebuilds them from the row and writes
them through the same compare-and-set commit path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tai42_contract.states.models import ApplyResult, CompletedOrigin, StateBatchWrite, StateSubject, WriteOrigin

from tai42_skeleton.states.outbox.models import OutboxItem, OutboxSubject


@dataclass(frozen=True)
class StagedReplace:
    """A whole-document replace staged in a unit, replayed through the facet's replace door at commit.

    Carried in the unit's ordered staging alongside the ``StateBatchWrite`` ops/template_jq items so a
    replace interleaves with them in authored order; the store is touched only when the save applies.
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


StagedWrite = StateBatchWrite | StagedReplace


def write_target(write: StagedWrite) -> tuple[str, StateSubject]:
    """The ``(state, subject)`` a staged write targets."""
    return write.state, write.subject


def outbox_item(write: StagedWrite, record: StagedItemRecord) -> OutboxItem:
    """The row item carrying one staged write and its stage record."""
    common: dict[str, Any] = {
        "completed_origin": record.completed_origin,
        "batch": record.batch,
        "applied_ops": record.applied_ops,
        "paths": record.paths,
        "ledger": record.ledger,
        "row": record.row,
        "provisional": record.provisional,
    }
    if isinstance(write, StagedReplace):
        return OutboxItem(
            kind="replace", state=write.state, subject=write.subject, data=write.data, origin=write.origin, **common
        )
    return OutboxItem(kind="batch", write=write, **common)


def _subject_id(state: str, subject: StateSubject) -> tuple[str, str, str, str, str]:
    return (state, subject.target_kind, subject.target_name, subject.kind, subject.key)


def staged_from_outbox(
    items: list[OutboxItem], subjects: list[OutboxSubject]
) -> tuple[list[StagedWrite], list[StagedItemRecord]]:
    """The staged writes and their stage records a pending save carries, in authored order."""
    by_subject = {_subject_id(s.state, s.subject): s for s in subjects}
    writes: list[StagedWrite] = []
    records: list[StagedItemRecord] = []
    for item in items:
        if item.kind == "batch":
            if item.write is None:
                raise ValueError("a batch outbox item carries its write")
            write: StagedWrite = item.write
            op_id = item.write.op_id
        else:
            if item.state is None or item.subject is None or item.data is None or item.origin is None:
                raise ValueError("a replace outbox item carries its state, subject, data and origin")
            write = StagedReplace(state=item.state, subject=item.subject, data=item.data, origin=item.origin)
            op_id = None
        state, subject = write_target(write)
        staged = by_subject.get(_subject_id(state, subject))
        if staged is None:
            raise ValueError(f"the outbox row names no subject entry for state {state!r} subject {subject}")
        writes.append(write)
        records.append(
            StagedItemRecord(
                applied_ops=item.applied_ops,
                paths=item.paths,
                op_id=op_id,
                batch=item.batch,
                completed_origin=item.completed_origin,
                ledger=item.ledger,
                row=item.row,
                version=staged.declaration_version,
                base_seq=staged.base_seq,
                document=staged.projected,
                provisional=item.provisional,
            )
        )
    return writes, records
