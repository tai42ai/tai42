"""The subject-keyed state store's wire models — the shapes every door and tool
read and write a subject's document through.

A *state* is a declared JSON document, one per *subject*. A subject is
``{target_kind, target_name, kind, key}``: the ``(target_kind, target_name)``
pair is the conversation-target scope (the only tenancy this platform has — the
pair a person never crosses), ``kind`` names the subject family the state
declares, and ``key`` addresses one subject within it. Kind ``person`` keys on a
person id from the identity store; every other kind is declared by the state.

These are the persisted/wire shapes only. The op/path engine, the effective-schema
composer, and the person/target validation are the skeleton's — this module pins
the models the door-agnostic facet passes across the seam, ``extra="forbid"`` so a
typo'd or stale key fails loudly at the boundary.

A JSON-Schema fragment travels under the wire key ``schema``; the Python attribute
is ``schema_`` (aliased) because a ``schema`` field would shadow
``BaseModel.schema`` — the read/write value is unchanged, only the attribute name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.conversation_target import ConversationTargetKind

#: A subject ``kind`` (and every entry of a declaration's ``subject_kinds``): a
#: lowercase identifier of at most 63 characters.
SUBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

#: The state ``name`` charset. No ``:`` — the fill op-id namespace-qualifies on
#: ``:``, so a name carrying one would let two ledger keys collide.
STATE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: The state-module ``name`` charset: a lowercase identifier of at most 63 chars.
MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

#: The one platform-validated kind: its key is a person id resolved against the
#: identity store, and the person's target must equal the subject's target.
PERSON_KIND = "person"

#: The upper bound on a record ``key`` length (the store column bound).
KEY_MAX_CHARS = 512

#: The ``retention_days`` upper bound — the store column is ``INTEGER`` (INT4), so a
#: larger value would raise a DB range error deep in a write instead of a clean,
#: early refusal.
MAX_RETENTION_DAYS = 2_147_483_647

#: The upper bound on a :class:`WriteOrigin` ``meta`` bag: the byte length of its compact
#: JSON serialization. A larger bag is refused loudly at the model boundary.
MAX_ORIGIN_META_BYTES = 4096

#: The literal every write door records: which door completed the write, stamped by
#: the platform chokepoint from the ambient context (never consumer-supplied).
StateDoor = Literal["conversation", "hook", "schedule", "tool", "api", "operator", "transfer"]


class StateSubject(BaseModel):
    """One addressed subject: the conversation-target scope
    ``(target_kind, target_name)`` plus the ``(kind, key)`` within it.

    Equality (and identity across every store method) is all four fields. ``key`` is
    stripped and must be 1..512 characters; ``kind`` matches :data:`SUBJECT_KIND_RE`.
    Frozen so a validated subject cannot be mutated after it is read back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    kind: str
    key: str

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if not SUBJECT_KIND_RE.fullmatch(value):
            raise ValueError(f"subject kind {value!r} must match {SUBJECT_KIND_RE.pattern}")
        return value

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("subject key must be non-blank")
        if len(trimmed) > KEY_MAX_CHARS:
            raise ValueError(f"subject key must be at most {KEY_MAX_CHARS} characters after trimming")
        return trimmed


class SubjectCandidates(BaseModel):
    """What a door knows about the subject before a state names its kind: the target
    scope and a ``by_kind`` map of the candidate keys the door resolved
    (``{"person": <id>, "thread": <thread_id>}``). A state's ambient subject is
    ``by_kind[declaration.default_subject_kind]``. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    by_kind: dict[str, str] = Field(default_factory=dict)


class StateContext(BaseModel):
    """The ambient execution-context snapshot one door deposits so every downstream
    write resolves its subject and provenance without a per-door argument.

    ONE generic object: the park carrier stores it whole, so a resumed run joins the
    same attribution later without a second field. ``door`` names the entering door,
    ``candidates`` the resolvable subjects, ``actor`` the accountable principal (a
    user id / execution key / ``None`` for system fires), ``turn_id`` the conversation
    turn, and ``inbound_id`` the inbound message the turn answers. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    door: StateDoor
    candidates: SubjectCandidates
    actor: str | None = None
    turn_id: str | None = None
    inbound_id: str | None = None


class WriteOrigin(BaseModel):
    """What a CONSUMER knows about a write — and nothing else.

    A consumer supplies its own ``consumer`` name, ``run_id`` (its run), ``op_id`` (its
    idempotency key), and an opaque ``meta`` bag: a JSON object the platform stores and
    echoes verbatim — never reading a key from it — in which a consumer keeps its own
    provenance (its own identifiers, say). ``door``, ``actor`` and ``turn_id`` are NOT here:
    they are stamped by the platform chokepoint from the ambient context, so the audit
    ledger cannot be forged by a consumer. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str | None = None
    meta: dict[str, Any] | None = None
    run_id: str | None = None
    op_id: str | None = None

    @field_validator("meta")
    @classmethod
    def _check_meta(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        try:
            encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"origin meta must be a JSON object: {exc}") from exc
        if len(encoded) > MAX_ORIGIN_META_BYTES:
            raise ValueError(f"origin meta is {len(encoded)} bytes serialized; the limit is {MAX_ORIGIN_META_BYTES}")
        return value


class CompletedOrigin(WriteOrigin):
    """A :class:`WriteOrigin` completed by the platform write chokepoint: the
    consumer's fields plus the ``door``, ``actor``, ``turn_id`` and ``inbound_id``
    stamped from :class:`StateContext`. This is the shape the ``state_writes`` ledger
    row and the ``_trace`` stamp carry. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    door: StateDoor
    actor: str | None = None
    turn_id: str | None = None
    inbound_id: str | None = None


class StateDeclaration(BaseModel):
    """A declared state: its ``name``, human ``description``, base JSON ``schema``
    (wire key ``schema``, attribute ``schema_``), the ``subject_kinds`` it serves
    (≥1, unique, each matching :data:`SUBJECT_KIND_RE`), the ``default_subject_kind``
    a door's ambient subject resolves to (one of ``subject_kinds``), and an optional
    ``retention_days`` (a positive INT4, or unset to keep records forever).

    ``effective_schema`` is the base ``schema`` composed with every mounted module's
    fragment, and ``regimes`` are the absolute write-regime rules composed over the
    mounts (each ``{path, regime}``, the mount path prefixed onto the module's regime
    paths). ``updated_at`` is the row's last-write timestamp. The platform computes all
    three and serves them on every read; a client that supplies a non-``None`` value for
    any of them on a write is refused (``… is set/computed by the platform``), so none can
    be forged across the wire."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    name: str
    description: str = ""
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    subject_kinds: list[str] = Field(min_length=1)
    default_subject_kind: str
    retention_days: int | None = Field(default=None, gt=0, le=MAX_RETENTION_DAYS)
    effective_schema: dict[str, Any] | None = None
    regimes: list[dict[str, Any]] | None = None
    updated_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not STATE_NAME_RE.fullmatch(value):
            raise ValueError(f"state name {value!r} must match {STATE_NAME_RE.pattern}")
        return value

    @field_validator("subject_kinds")
    @classmethod
    def _check_subject_kinds(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("subject_kinds must be unique")
        for kind in value:
            if not SUBJECT_KIND_RE.fullmatch(kind):
                raise ValueError(f"subject kind {kind!r} must match {SUBJECT_KIND_RE.pattern}")
        return value

    @model_validator(mode="after")
    def _check_default_in_kinds(self) -> StateDeclaration:
        if self.default_subject_kind not in self.subject_kinds:
            raise ValueError(
                f"default_subject_kind {self.default_subject_kind!r} must be one of subject_kinds {self.subject_kinds}"
            )
        return self


class StateModuleDocument(BaseModel):
    """A state-module document: the reusable schema fragment plus the parameters, write
    regimes, mount-time ``declarations`` and ``trace`` switch the platform owns.
    ``extra="forbid"`` refuses any key outside these — a consumer keeps its own documents
    (its views, predicates, or whatever it needs) beside the module under its own kind,
    validated through its registered mount validator, never folded into this shape.

    The wire key ``schema`` is the attribute ``schema_`` (alias). Structural
    validation of the fragment, regime paths and declarations lives at the store."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    kind: Literal["state-module"] = "state-module"
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    regimes: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    declarations: dict[str, Any] | None = None
    trace: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not MODULE_NAME_RE.fullmatch(value):
            raise ValueError(f"module name {value!r} must match {MODULE_NAME_RE.pattern}")
        return value


class MountBody(BaseModel):
    """A mount request: the ``path`` in the state's document where the module's
    fragment lands, plus the mount's parameter values and static ``declarations``."""

    model_config = ConfigDict(extra="forbid")

    path: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    declarations: dict[str, Any] = Field(default_factory=dict)


class RecordView(BaseModel):
    """A read record: the ``state``, its ``subject``, the ``data`` document and its
    monotonic ``seq``, plus the ``canonical_subject`` a fold resolved the subject to
    and every subject ``folded_from`` into it. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    subject: StateSubject
    data: dict[str, Any] = Field(default_factory=dict)
    seq: float
    canonical_subject: StateSubject
    folded_from: list[StateSubject] = Field(default_factory=list[StateSubject])


class ApplyResult(BaseModel):
    """The outcome of an ``apply``: whether it ``applied`` (a replayed op-id or an
    empty batch is ``False``; a batch whose every op guard-skips still reports
    ``applied=True`` with the ops in ``skipped``), the resulting ``data`` and ``seq``
    when it did, and the ``skipped`` ops (each a reason record) a guard held back."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    data: dict[str, Any] | None = None
    seq: float | None = None
    skipped: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


class WriteEntry(BaseModel):
    """One row of a subject's audit trail: the ``seq`` and timestamp ``at`` of the
    write, the :class:`CompletedOrigin` that produced it, and the absolute ``paths``
    it touched (each a list of string keys / integer list indices)."""

    model_config = ConfigDict(extra="forbid")

    seq: float
    at: datetime
    origin: CompletedOrigin
    paths: list[list[str | int]] = Field(default_factory=list[list[str | int]])


class WritesPage(BaseModel):
    """One keyset page of a subject's write ledger: the ``items`` (newest first) and the
    ``next_cursor`` a caller feeds the next call. ``next_cursor`` is the last row's id (a
    string) when the page is full, else ``None`` — the page-model idiom the subject and
    search pages share, so a caller pages the audit trail the same way it pages them."""

    model_config = ConfigDict(extra="forbid")

    items: list[WriteEntry] = Field(default_factory=list[WriteEntry])
    next_cursor: str | None = None


class ConsumerLink(BaseModel):
    """Where the Studio opens a consumer: a feature ``token`` + ``search`` (a states
    page → hooks/scheduling/agents cross-link) OR a ``plugin_path`` + ``search`` (a
    plugin's own screen). All optional — a consumer that renders no link supplies
    none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str | None = None
    plugin_path: str | None = None
    search: dict[str, Any] | None = None


class ConsumerRow(BaseModel):
    """One thing that binds a state, as the Consumers tab reads it: its ``kind``
    (hook / schedule / agent / a consumer plugin's own kind), ``name``, human ``detail`` and optional
    ``link``. ``unavailable`` (mutually exclusive with the rest) marks a consumer
    family that cannot be listed on this deployment (e.g. no scheduling backend),
    surfaced as a muted line — never swallowed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    name: str | None = None
    detail: str | None = None
    link: ConsumerLink | None = None
    unavailable: str | None = None


#: A data-dependent mount validator a consumer registers on the states facet: given
#: the module document, the mount's declaration VALUES, and the state's effective
#: schema, it RAISES loudly (a ``ModuleValidationError`` naming the offending item)
#: to refuse the door — consulted before every mount / declarations write. A pass
#: returns ``None``.
MountValidator = Callable[["StateModuleDocument", dict[str, Any], dict[str, Any]], Awaitable[None]]

#: A consumer lister a plugin registers per consumer ``kind``: given a state name,
#: returns the :class:`ConsumerRow` rows for that kind. The facet's ``consumers``
#: unions every registered lister.
ConsumerLister = Callable[[str], Awaitable[Sequence[ConsumerRow]]]
