"""The pending-save shapes a state scan or a re-declare answers with.

A run's validated writes and deferred calls are saved durably before the reply and applied after
it, in per-subject order. A save that failed holds its subjects until an operator retries or
discards it; the scans that keep serving over committed data name the held saves they skipped.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.states.models import StateDeclaration, StateSubject


class HeldPendingSave(BaseModel):
    """One outstanding save whose subjects are held by a failed save.

    ``save_id`` names the held save, ``held_by`` the failed save holding it (equal to ``save_id``
    for the failed save itself), and ``subjects`` the subjects it writes. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    save_id: str
    held_by: str
    subjects: list[StateSubject] = Field(default_factory=list[StateSubject])


class PruneResult(BaseModel):
    """The retention sweep's answer: the per-state record removal counts and the held saves it skipped."""

    model_config = ConfigDict(extra="forbid")

    pruned: dict[str, int] = Field(default_factory=dict[str, int])
    held: list[HeldPendingSave] = Field(default_factory=list[HeldPendingSave])


class StateDeclarationSaved(BaseModel):
    """A declare's answer: the stored ``declaration`` and the held saves it was accepted beside. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    declaration: StateDeclaration
    held: list[HeldPendingSave] = Field(default_factory=list[HeldPendingSave])
