"""The JSON shapes a ``state_outbox`` row carries, and the in-memory row the outbox works on.

A row stores a unit's staging serialized: every staged write with what it recorded at stage
(``OutboxItem``), every touched subject with the base it was staged against and its projected
document (``OutboxSubject``), and every deferred call (``OutboxCall``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from tai42_contract.states.models import (
    ApplyResult,
    CompletedOrigin,
    StateBatchWrite,
    StateSubject,
    WriteOrigin,
)

RowStatus = Literal["pending", "calls", "running", "failed"]
FailedPhase = Literal["records", "calls"]


class OutboxItem(BaseModel):
    """One staged write: a batch item (``write``) or a whole-document replace, with its stage record."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["batch", "replace"]
    write: StateBatchWrite | None = None
    state: str | None = None
    subject: StateSubject | None = None
    data: dict[str, Any] | None = None
    origin: WriteOrigin | None = None
    completed_origin: CompletedOrigin
    batch: int
    applied_ops: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    paths: list[list[Any]] = Field(default_factory=list[list[Any]])
    ledger: bool
    row: bool
    provisional: ApplyResult


class OutboxSubject(BaseModel):
    """One staged subject: its canonical and aliases, the base it was staged against, its projection."""

    model_config = ConfigDict(extra="forbid")

    state: str
    subject: StateSubject
    canonical: StateSubject
    aliases: list[StateSubject] = Field(default_factory=list[StateSubject])
    base_seq: float | None
    declaration_version: int
    projected: dict[str, Any] | None


class OutboxCall(BaseModel):
    """One deferred call: its registered ``kind``, what it calls (``target``), the kind's payload, the caller's run."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    target: str
    payload: dict[str, Any]
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """A ``state_outbox`` row as read."""

    id: int
    status: RowStatus
    record_keys: list[str]
    subject_keys: list[str]
    targets: list[str]
    states: list[str]
    run_id: str | None
    trace_id: str | None
    records: list[OutboxItem]
    subjects: list[OutboxSubject]
    calls: list[OutboxCall]
    calls_done: int
    attempts: int
    next_attempt_at: datetime | None
    claimed_by: str | None
    lease_until: datetime | None
    last_error: str | None
    failed_phase: FailedPhase | None
    created_at: datetime
    records_applied_at: datetime | None
    failed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RowApply:
    """What one apply of a row's record part came to.

    ``applied`` — this call applied it; ``not_pending`` — gone or past its record part already;
    ``held`` — an older failed row (``held_by``) shares one of its record keys; ``failed`` — the
    apply failed deterministically and the row is now failed; ``retry`` — a transient failure,
    the row is pending with a backoff; ``contended`` — a key lock was not granted in time. ``error``
    carries the failure for ``failed`` / ``retry`` / ``contended``.
    """

    outcome: Literal["applied", "not_pending", "held", "failed", "retry", "contended"]
    held_by: int | None = None
    error: BaseException | None = None


class OutboxPending(Exception):  # noqa: N818 - a control signal between the store and the service, not an error
    """A record read or write met a subject with an unapplied pending save.

    Raised by the store on a pooled read; the service drains ``keys`` and runs the call again.
    It never leaves the service.
    """

    def __init__(self, keys: list[str]) -> None:
        """Carry the record ``keys`` the store found pending."""
        super().__init__(f"pending state save on {keys}")
        self.keys = keys


@dataclass(frozen=True, slots=True)
class HeldRecord:
    """A record a held save writes when it applies: the re-declare guard counts it as present."""

    save_id: str
    held_by: str
    subject: StateSubject
    document: dict[str, Any]
