"""The callables a consumer registers on the states facet.

An attach validator and an attach reconciler, consulted on every attach and
declarations write (the reconciler through the narrow record door bound to the
state), and a consumer lister per consumer ``kind``, unioned by the facet's
``consumers`` read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from tai42_contract.states.models import (
    ApplyResult,
    ConsumerRow,
    StateRecord,
    StateSubject,
    StateTemplateDocument,
    WriteOrigin,
)

#: A data-dependent attach validator a consumer registers on the states facet: given the
#: state name, the template document, the attachment's declaration VALUES, and the state's
#: effective schema, it RAISES loudly (a ``TemplateValidationError`` naming the offending item)
#: to refuse the door — consulted before every attach / declarations write and every template
#: replace, once per live attachment. A pass returns ``None``.
AttachValidator = Callable[[str, "StateTemplateDocument", dict[str, Any], dict[str, Any]], Awaitable[None]]


@runtime_checkable
class AttachReconcileRecords(Protocol):
    """The narrow record door an attach reconciler reads and writes a (re)attached state through.

    A subset of the states facet bound to the one state: read a subject, page its subjects,
    ``merge`` a shallow resolution, or ``apply`` a full op batch (the same keyed ops as an
    update-purpose program, so a record under a ``composing`` write regime can be closed too). Every
    write runs on the attach transaction and is completed and audited at the platform chokepoint
    exactly like a facet write.
    """

    async def read(self, subject: StateSubject) -> StateRecord | None:
        """Return the state's record for ``subject``, or ``None`` when it has none."""
        ...

    async def list_subjects(
        self, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Return one keyset page of the state's subjects, optionally filtered by ``kind``."""
        ...

    async def merge(self, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin) -> StateRecord:
        """Merge ``patch`` into ``subject``'s record and return the updated record."""
        ...

    async def apply(self, subject: StateSubject, ops: list[dict[str, Any]], *, origin: WriteOrigin) -> ApplyResult:
        """Apply ``ops`` to ``subject``'s record and return the :class:`ApplyResult`."""
        ...


@dataclass(frozen=True, kw_only=True)
class AttachReconcileContext:
    """What an attach reconciler receives before an attach write commits.

    The ``state`` name, the ``template`` document, the ``operation`` replacing declarations, the
    ``previous_declarations`` (``None`` on a first attach), the ``new_declarations``, the
    attachment ``options``, and the ``records`` door bound to the state. A reconciler RAISES a
    :class:`~tai42_contract.states.errors.TemplateValidationError` to refuse the attach
    (naming the offending records) or writes resolutions through ``records`` and returns,
    letting the attach commit with those writes.
    """

    state: str
    template: StateTemplateDocument
    operation: Literal["attach", "update_declarations"]
    previous_declarations: dict[str, Any] | None
    new_declarations: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict[str, Any])
    records: AttachReconcileRecords


#: A pre-write attach reconciler a consumer registers on the states facet: given a
#: :class:`AttachReconcileContext`, it reconciles the state's OPEN records against the new
#: declarations — RAISING (a ``TemplateValidationError`` naming the records) to refuse the
#: attach, or writing resolutions through the context's record door and returning to let
#: the attach commit. Run inside every attach / declarations write, after the validators and
#: before the write.
AttachReconciler = Callable[["AttachReconcileContext"], Awaitable[None]]

#: A consumer lister a plugin registers per consumer ``kind``: given a state name,
#: returns the :class:`ConsumerRow` rows for that kind. The facet's ``consumers``
#: unions every registered lister.
ConsumerLister = Callable[[str], Awaitable[Sequence[ConsumerRow]]]
