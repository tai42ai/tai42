"""Response models for the states pending-save outbox operations (``/api/state-pending-saves*``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from tai42_contract.states import (
    StateSubject,
)


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
