"""The shapes a record-validation or record-check answers with.

A record document is validated against its state's effective schema (base ∘ attachments). The
answer is every way it does not fit, each a JSON path and the validator's reason, so a misfit is
one answer rather than one-failure-at-a-time. The stored-records check reuses the same reasons,
naming the subject and last-write time of each record that does not fit.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.states.models import StateSubject


class ValidationReason(BaseModel):
    """One way a record document does not fit a state's effective schema.

    ``path`` is the JSON path of the offending node (``$`` for the document root) and
    ``message`` the schema validator's reason. A document that fits has none. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    message: str


class RecordValidation(BaseModel):
    """Whether a candidate document fits a state's effective schema, with every reason it does not.

    ``fits`` is true exactly when ``reasons`` is empty; ``reasons`` lists EVERY way the document
    fails, ordered by JSON path, so a misfit is one answer rather than one-failure-at-a-time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fits: bool
    reasons: list[ValidationReason] = Field(default_factory=list[ValidationReason])


class RecordCheckItem(BaseModel):
    """One stored record that does not fit its state's current effective schema.

    The ``subject`` it belongs to, the ``updated_at`` epoch seconds of its last write, and every
    ``reasons`` entry it fails on (ordered by JSON path). Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: StateSubject
    updated_at: float
    reasons: list[ValidationReason] = Field(default_factory=list[ValidationReason])
