"""Response models for the states operations (``/api/states*``, ``/api/state-modules*``
and ``/api/state-retention/prune``).

Each model DESCRIBES the inner payload a states operation returns today — the shape the
route adapter wraps in the ``{"data": ...}`` success envelope — and never re-declares the
envelope or reshapes a wire body. The persisted wire shapes (``StateDeclaration``,
``StateModuleDocument``, ``RecordView``, ``ApplyResult``, ``WritesPage``, ``ConsumerRow``,
``StateSubject``) live in :mod:`tai42_contract.states` and are imported and reused (or
extended) here, never re-authored. A genuinely-open JSON-Schema fragment (a state's base
or effective schema, a mount's resolved parameters/declarations, a composed regime rule)
is typed ``JsonValue`` — it is arbitrary per-deployment JSON, not a fixed platform shape.
Bare-list bodies are NAMED ``RootModel`` subclasses so the offline emitter registers each
under a stable, unique ``__name__``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel
from tai42_contract.states import (
    ConsumerRow,
    RecordView,
    StateDeclaration,
    StateModuleDocument,
    StateSubject,
)

# --------------------------------------------------------------------------- #
# Declarations                                                                 #
# --------------------------------------------------------------------------- #


class StateDeclarationList(RootModel[list[StateDeclaration]]):
    """The bare-list body of ``list_states`` — every declared state as its full
    ``StateDeclaration`` (base + composed effective schema, regimes and ``updated_at``
    included)."""


class StateMountView(BaseModel):
    """One module mounted on a state, as the served declaration read carries it: the
    ``module`` name, the ``path`` in the document where its fragment lands, and the
    mount's resolved ``parameters`` and static ``declarations`` (both arbitrary
    per-module JSON)."""

    module: str
    path: list[str]
    parameters: dict[str, JsonValue]
    declarations: dict[str, JsonValue]


class ServedStateView(BaseModel):
    """The full served declaration read of ``get_state``: the base ``schema`` and the
    composed ``effective_schema`` (both open JSON-Schema objects), the ``subject_kinds``
    the state serves and its ``default_subject_kind``, an optional ``retention_days``
    (``null`` to keep records forever), the state's ``mounts``, the absolute write-regime
    rules ``regimes`` (each ``{path, regime}``), and ``updated_at`` (the ISO timestamp of
    the last write, ``null`` before any). The Python attribute for the wire ``schema`` key
    is suffixed to avoid shadowing a ``BaseModel`` member; the wire key stays ``schema``
    via the alias."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str
    schema_: dict[str, JsonValue] = Field(alias="schema")
    effective_schema: dict[str, JsonValue]
    subject_kinds: list[str]
    default_subject_kind: str
    retention_days: int | None
    mounts: list[StateMountView]
    regimes: list[dict[str, JsonValue]]
    updated_at: str | None


class StateDeleteResult(BaseModel):
    """A state (or state-module) delete confirmation — ``deleted`` and the ``name``
    removed."""

    deleted: bool
    name: str


class StateStats(BaseModel):
    """A state's record statistics: the total ``records`` count, the ``per_field`` and
    ``per_kind`` breakdowns (each a name-keyed count map), and the number of live
    ``consumers`` binding the state."""

    records: int
    per_field: dict[str, int]
    per_kind: dict[str, int]
    consumers: int


# --------------------------------------------------------------------------- #
# Mounts                                                                       #
# --------------------------------------------------------------------------- #


class StateMountRow(StateMountView):
    """One mount as the ``list_state_mounts`` listing carries it — the served
    :class:`StateMountView` fields plus the ``state`` the mount sits on."""

    state: str


class StateMountList(RootModel[list[StateMountRow]]):
    """The bare-list body of ``list_state_mounts`` — every module mounted on the
    state."""


class MountAck(BaseModel):
    """A module-mount confirmation — the ``state`` and ``module`` mounted."""

    mounted: bool
    state: str
    module: str


class MountUpdateAck(BaseModel):
    """A mount-declarations update confirmation — the ``state`` and ``module``
    updated."""

    updated: bool
    state: str
    module: str


class UnmountAck(BaseModel):
    """A module-unmount confirmation — the ``state`` and ``module`` unmounted."""

    unmounted: bool
    state: str
    module: str


# --------------------------------------------------------------------------- #
# Subjects + records                                                           #
# --------------------------------------------------------------------------- #


class StateSubjectEntry(BaseModel):
    """One subject holding a record for the state: its ``subject`` identity and the
    ``updated_at`` epoch seconds of its last write."""

    subject: StateSubject
    updated_at: float


class StateSubjectsPage(BaseModel):
    """One keyset page of a state's subjects (``list_state_subjects``). ``next_cursor``
    is the cursor the next page reads from, ``null`` on the last page."""

    subjects: list[StateSubjectEntry]
    next_cursor: str | None


class StateSearchPage(BaseModel):
    """One keyset page of a state's containment-matched records (``search_state_records``)
    — the matching subjects. ``next_cursor`` is ``null`` on the last page."""

    matches: list[StateSubjectEntry]
    next_cursor: str | None


class StateRecordOrNull(RootModel[RecordView | None]):
    """The ``read_state_record`` body: one subject's record as a ``RecordView``, or
    ``null`` when the subject holds none."""


class EraseAck(BaseModel):
    """A subject-record erase confirmation."""

    erased: bool


class FoldSubjectRef(BaseModel):
    """One end of a fold — the ``kind`` and ``key`` of a subject."""

    kind: str
    key: str


class FoldReport(BaseModel):
    """The ``fold_state_record`` report: the fold ``mode``, the ``from`` and ``into``
    subjects, whether the fold was ``already`` in place (a quiet no-op), and the number
    of aliases ``flattened`` onto the survivor. ``merged_members`` — the survivor's
    newly-filled top-level members — rides ONLY a ``merge`` fold; a ``switch`` omits it.
    The Python attribute for the wire ``from`` key is suffixed (``from`` is a keyword);
    the wire key stays ``from`` via the alias."""

    model_config = ConfigDict(populate_by_name=True)

    mode: str
    from_: FoldSubjectRef = Field(alias="from")
    into: FoldSubjectRef
    already: bool
    flattened: int
    merged_members: list[str] | None = None


class StateConsumerList(RootModel[list[ConsumerRow]]):
    """The bare-list body of ``state_consumers`` — everything that binds the state (flows,
    hooks, schedules, agents), each a ``ConsumerRow`` (an unlistable consumer family is a
    labelled ``unavailable`` row)."""


# --------------------------------------------------------------------------- #
# Modules (the sibling collection) + retention                                #
# --------------------------------------------------------------------------- #


class StateModuleCatalogEntry(StateModuleDocument):
    """One state-module document in the catalog listing — the stored
    :class:`StateModuleDocument` plus ``mounted_on`` (the number of states it is mounted
    on) and ``shipped_default`` (true when it is an unedited shipped default)."""

    mounted_on: int
    shipped_default: bool


class StateModuleCatalog(RootModel[list[StateModuleCatalogEntry]]):
    """The bare-list body of ``list_state_modules`` — every platform state-module document
    with its catalog columns."""


class PruneResult(BaseModel):
    """The ``prune_state_retention`` report: ``pruned`` maps each state whose records were
    swept to the number deleted (empty when nothing was past its horizon)."""

    pruned: dict[str, int]
