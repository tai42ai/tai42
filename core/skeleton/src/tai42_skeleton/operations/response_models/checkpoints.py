"""Response models for the checkpoints operations."""

from __future__ import annotations

from pydantic import BaseModel


class CheckpointSweepResult(BaseModel):
    """The checkpoint-sweep report.

    ``finished_swept`` / ``waiting_swept`` list the threads deleted past each horizon;
    ``spared`` lists the threads past a horizon a live-thread filter reported live.
    ``skipped`` is set only when the provider has no waiting-horizon sweep (its threads
    expire by key TTL, or live as long as the process). ``waiting_minutes`` /
    ``finished_minutes`` are the platform's two retention values: the default and the
    ceiling of every thread's retention, which a thread's owner may declare shorter.
    """

    provider: str
    waiting_minutes: int
    finished_minutes: int
    swept_count: int
    finished_swept: list[str]
    waiting_swept: list[str]
    spared: list[str]
    skipped: str | None = None
