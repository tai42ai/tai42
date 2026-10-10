"""Response models for the states operations.

Cover ``/api/states*``, ``/api/state-templates*``, ``/api/state-retention/prune`` and
``/api/state-pending-saves*``.

Each model DESCRIBES the inner payload a states operation returns — the shape the
route adapter wraps in the ``{"data": ...}`` success envelope — and never re-declares the
envelope or reshapes a wire body. The persisted wire shapes (``StateDeclaration``,
``StateTemplateDocument``, ``StateRecord``, ``ApplyResult``, ``WritesPage``, ``ConsumerRow``,
``StateSubject``) live in :mod:`tai42_contract.states` and are imported and reused (or
extended) here, never re-authored. A genuinely-open JSON-Schema fragment (a state's base
or effective schema, an attachment's resolved parameters/declarations, a composed regime
rule) is typed ``JsonValue`` — it is arbitrary per-deployment JSON, not a fixed platform
shape. Bare-list bodies are NAMED ``RootModel`` subclasses so the offline emitter registers
each under a stable, unique ``__name__``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel
from tai42_contract.states import (
    ConsumerRow,
    HeldPendingSave,
    StateDeclaration,
    StateRecord,
    StateSubject,
    StateTemplateDocument,
)

# --------------------------------------------------------------------------- #
# Declarations                                                                 #
# --------------------------------------------------------------------------- #


class HeldPendingSaveList(RootModel[list[HeldPendingSave]]):
    """The saves held by a failed pending save that a door read past or was accepted beside.

    Each names the held save, the failed save holding it, and the subjects it writes.
    """


class StateDeclarationList(RootModel[list[StateDeclaration]]):
    """The bare-list body of ``list_states`` — every declared state as its full ``StateDeclaration``.

    Base + composed effective schema, regimes and ``updated_at`` included.
    """


class StateAttachmentView(BaseModel):
    """One template attached on a state, as the served declaration read carries it.

    The ``template`` name, the ``path`` in the document where its fragment lands, and the
    attachment's resolved ``parameters`` and static ``declarations`` (both arbitrary per-template JSON).
    """

    template: str
    path: list[str]
    parameters: dict[str, JsonValue]
    declarations: dict[str, JsonValue]


class ServedStateView(BaseModel):
    """The full served declaration read of ``get_state``.

    The base ``schema`` and the
    composed ``effective_schema`` (both open JSON-Schema objects), the ``subject_kinds``
    the state serves and its ``default_subject_kind``, an optional ``retention_days``
    (``null`` to keep records forever), the state's ``attachments``, the absolute write-regime
    rules ``regimes`` (each ``{path, regime}``), and ``updated_at`` (the ISO timestamp of
    the last write, ``null`` before any). The Python attribute for the wire ``schema`` key
    is suffixed to avoid shadowing a ``BaseModel`` member; the wire key stays ``schema``
    via the alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str
    schema_: dict[str, JsonValue] = Field(alias="schema")
    effective_schema: dict[str, JsonValue]
    subject_kinds: list[str]
    default_subject_kind: str
    retention_days: int | None
    attachments: list[StateAttachmentView]
    regimes: list[dict[str, JsonValue]]
    updated_at: str | None


class StateDeleteResult(BaseModel):
    """A state (or state-template) delete confirmation — ``deleted`` and the ``name`` removed."""

    deleted: bool
    name: str


class StateStats(BaseModel):
    """A state's record statistics.

    The total ``records`` count, the ``per_field`` and ``per_kind`` breakdowns (each a name-keyed count map),
    and the number of live ``consumers`` binding the state.
    """

    records: int
    per_field: dict[str, int]
    per_kind: dict[str, int]
    consumers: int
    held: HeldPendingSaveList = Field(
        default_factory=lambda: HeldPendingSaveList([]),
        description="Saves held by a failed pending save; the counts are over committed records and exclude them.",
    )


class StateDeclarationPutResponse(StateDeclaration):
    """The stored declaration a create or re-declare answers, with the held saves it was accepted beside."""

    held: HeldPendingSaveList = Field(default_factory=lambda: HeldPendingSaveList([]))


# --------------------------------------------------------------------------- #
# Attachments                                                                  #
# --------------------------------------------------------------------------- #


class StateAttachmentRow(StateAttachmentView):
    """One attachment as the ``list_state_attachments`` listing carries it.

    The served :class:`StateAttachmentView` fields plus the ``state`` the attachment sits on.
    """

    state: str


class StateAttachmentList(RootModel[list[StateAttachmentRow]]):
    """The bare-list body of ``list_state_attachments`` — every template attached on the state."""


class AttachAck(BaseModel):
    """A template-attach confirmation — the ``state`` and ``template`` attached.

    ``held`` names the saves held by a failed pending save whose subjects were read over their committed records.
    """

    attached: bool
    state: str
    template: str
    held: HeldPendingSaveList = Field(default_factory=lambda: HeldPendingSaveList([]))


class AttachUpdateAck(BaseModel):
    """An attachment-declarations update confirmation — the ``state`` and ``template`` updated.

    ``held`` names the saves held by a failed pending save whose subjects were read over their committed records.
    """

    updated: bool
    state: str
    template: str


class DetachAck(BaseModel):
    """A template-detach confirmation — the ``state`` and ``template`` detached."""

    detached: bool
    state: str
    template: str


# --------------------------------------------------------------------------- #
# Subjects + records                                                           #
# --------------------------------------------------------------------------- #


class StateSubjectEntry(BaseModel):
    """One subject holding a record for the state.

    Its ``subject`` identity and the ``updated_at`` epoch seconds of its last write.
    """

    subject: StateSubject
    updated_at: float


class StateSubjectsPage(BaseModel):
    """One keyset page of a state's subjects (``list_state_subjects``).

    ``next_cursor`` is the cursor the next page reads from, ``null`` on the last page.
    """

    subjects: list[StateSubjectEntry]
    next_cursor: str | None
    held: HeldPendingSaveList = Field(
        default_factory=lambda: HeldPendingSaveList([]),
        description="Saves held by a failed pending save: their subjects show their last applied data.",
    )


class StateSearchPage(BaseModel):
    """One keyset page of a state's containment-matched records (``search_state_records``) — the matching subjects.

    ``next_cursor`` is ``null`` on the last page.
    """

    matches: list[StateSubjectEntry]
    next_cursor: str | None
    held: HeldPendingSaveList = Field(
        default_factory=lambda: HeldPendingSaveList([]),
        description="Saves held by a failed pending save: their subjects are matched on their last applied data.",
    )


class StateRecordOrNull(RootModel[StateRecord | None]):
    """The ``read_state_record`` body: one subject's record as a ``StateRecord``, or ``null`` when it holds none."""


class EraseAck(BaseModel):
    """A subject-record erase confirmation."""

    erased: bool


class FoldSubjectRef(BaseModel):
    """One end of a fold — the ``kind`` and ``key`` of a subject."""

    kind: str
    key: str


class FoldReport(BaseModel):
    """The ``fold_state_record`` report.

    The fold ``mode``, the ``from`` and ``into`` subjects, whether the fold was ``already`` in place (a quiet
    no-op), and the number of aliases ``flattened`` onto the survivor. ``merged_members`` — the survivor's
    newly-filled top-level members — rides ONLY a ``merge`` fold; a ``switch`` omits it.
    The Python attribute for the wire ``from`` key is suffixed (``from`` is a keyword);
    the wire key stays ``from`` via the alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    mode: str
    from_: FoldSubjectRef = Field(alias="from")
    into: FoldSubjectRef
    already: bool
    flattened: int
    merged_members: list[str] | None = None


class StateConsumerList(RootModel[list[ConsumerRow]]):
    """The bare-list body of ``state_consumers`` — everything that binds the state.

    Each is a ``ConsumerRow`` (hooks, schedules, agents, and any consumer engine); an unlistable consumer family
    is a labelled ``unavailable`` row.
    """


# --------------------------------------------------------------------------- #
# Templates (the sibling collection) + retention                              #
# --------------------------------------------------------------------------- #


class StateTemplateCatalogEntry(StateTemplateDocument):
    """One state-template document in the catalog listing.

    The stored :class:`StateTemplateDocument` plus ``attached_to`` (the number of states it is attached
    on) and ``shipped_default`` (true when it is an unedited shipped default).
    """

    attached_to: int
    shipped_default: bool


class StateTemplateCatalog(RootModel[list[StateTemplateCatalogEntry]]):
    """The bare-list body of ``list_state_templates`` — every platform state-template document with catalog columns."""


class PruneResult(BaseModel):
    """The ``prune_state_retention`` report.

    ``pruned`` maps each state whose records were swept to the number deleted (empty when nothing was past its
    horizon); ``held`` names the saves held by a failed pending save whose subjects kept their records.
    """

    pruned: dict[str, int]
    held: HeldPendingSaveList = Field(default_factory=lambda: HeldPendingSaveList([]))


# --------------------------------------------------------------------------- #
# Pending saves                                                                #
# --------------------------------------------------------------------------- #


class StatePendingSaveSubject(BaseModel):
    """One subject a pending save writes, under its ``state``."""

    state: str
    subject: StateSubject


class StatePendingSaveCall(BaseModel):
    """One call a pending save runs after its records apply: its call ``kind`` and what it calls."""

    kind: str
    target: str


class StatePendingSave(BaseModel):
    """One outstanding pending state save, without its record data or call arguments.

    ``status`` is ``pending`` (records not yet applied), ``calls`` (records applied, calls queued),
    ``running`` (a call is running) or ``failed`` (held until an operator retries or discards it;
    ``failed_phase`` says which part failed).
    """

    id: str
    status: Literal["pending", "calls", "running", "failed"]
    run_id: str | None
    states: list[str]
    subjects: list[StatePendingSaveSubject]
    calls: list[StatePendingSaveCall]
    attempts: int
    last_error: str | None
    failed_phase: Literal["records", "calls"] | None
    created_at: datetime
    failed_at: datetime | None


class StatePendingSavesPage(BaseModel):
    """One page of pending saves, newest first, with the totals of every outstanding and every failed save.

    ``next_cursor`` is the cursor the next page reads from, ``null`` on the last page.
    """

    items: list[StatePendingSave]
    next_cursor: str | None
    outstanding: int
    failed: int


class StatePendingSaveRetried(BaseModel):
    """A retried pending save's state after its records phase.

    ``applied`` — it landed whole and is gone; ``pending`` — it waits behind an older save or a
    contended subject; ``calls`` / ``running`` — its records applied and its calls are queued or
    running; ``failed`` — it failed again (``last_error`` says why).
    """

    id: str
    status: Literal["applied", "pending", "calls", "running", "failed"]
    last_error: str | None


class StatePendingSaveDiscarded(BaseModel):
    """A discarded pending save's id."""

    discarded: str
