"""The ``ask`` answer and interaction-state models.

``InteractionResponse`` is the validated answer pushed onto the reply channel;
``InteractionState`` is the mutable record the answer endpoint reads;
``SuspendedInteraction`` is the sentinel an ``async`` ask returns in place of an answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tai42_contract.entry_params import validate_entry_params
from tai42_contract.interactions.models.request import InteractionRequest


class InteractionResponse(BaseModel):
    """The answer. Validated server-side before it wakes the caller.

    ``params`` is the OPTIONAL opaque channel enrichment carried WITH the answer — the answer-path
    counterpart of the bridge path's :class:`~tai42_contract.channels.InboundBridge.params` and a
    conversation entry's ``params``: when a tap/reply that ANSWERS a pending ask also carries
    channel-specific context (a tapped reply id, a template button payload, a referral, the
    reply-to context the participant quoted) the channel encodes it as string entries here so the asking
    flow reads them beside ``answer``. The SAME transport vocabulary
    (:func:`~tai42_contract.entry_params.validate_entry_params`) bounds it as every params seam;
    the platform attaches no meaning and NO TRUST. ``None`` means no enrichment — a plain answer,
    byte-identical to the pre-params envelope.
    """

    interaction_id: str
    answer: Any
    answered_by: str
    answered_at: datetime
    params: dict[str, str] | None = None

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return validate_entry_params(value)


class InteractionState(BaseModel):
    """Mutable companion to the immutable stream entry, keyed by interaction id."""

    status: Literal["pending", "answered"]
    group_id: str
    request: InteractionRequest
    response: InteractionResponse | None = None


class SuspendedInteraction(BaseModel):
    """The sentinel an ``async`` ask returns in place of an answer.

    Any async-suspending tool returns it to signal the caller was parked, keyed by
    ``interaction_id`` so the resume path can find the parked question. Generic:
    a resuming driver's resume state is keyed by this id, not carried here.
    """

    interaction_id: str
    expiry_at: datetime | None = None
    # The resume continuation this park was raised UNDER — the one driver entitled to ADOPT
    # it as its own park (see ``assert_park_adoptable``). A park has exactly one resume owner,
    # so a caller that turns a returned sentinel into its own park state must be that owner.
    # An ask that parks always stamps it, so ``None`` means the sentinel was NOT minted by an
    # ask: a nested RUN's park surfaced at its tool face, which no caller may adopt.
    resume_owner: str | None = None
    # Every ask this park represents. For a single ask it is ``[interaction_id]`` (filled by
    # default); a driver that surfaces a whole super-step at one tool face MERGES the
    # per-ask sentinels' lists here.
    interaction_ids: list[str] = Field(default_factory=list)
    # The subset of ``interaction_ids`` addressed to the CALLER (``to="caller"``). Empty for
    # a park of only user asks; a driver merges the per-ask subsets when it surfaces a step.
    caller_interaction_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_interaction_ids(self) -> SuspendedInteraction:
        # A sentinel always represents at least its own ask, so an unset ``interaction_ids``
        # defaults to the single id; ``caller_interaction_ids`` stays a caller-only subset.
        if not self.interaction_ids:
            self.interaction_ids = [self.interaction_id]
        return self

    @field_validator("expiry_at")
    @classmethod
    def _ensure_expiry_tz_aware(cls, value: datetime | None) -> datetime | None:
        # The async park deadline is compared against an aware ``now()`` by the
        # expiry reaper; a naive value would raise TypeError there. None stays None
        # (a sync question carries no deadline); a set value is reject-naive +
        # UTC-normalized, the same strictness as its ``InteractionRequest`` sibling.
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class ResumeBuffered(BaseModel):
    """A resume that did NOT drive its super-step to a terminal: sibling asks are still open.

    One super-step of a held run can park SEVERAL asks in parallel. Answering one of them
    buffers that answer and leaves the step suspended until the last sibling resolves. A resuming
    driver returns this in place of a terminal to say exactly that: ``remaining_ids`` are the
    still-unanswered asks of the same step. It is NOT terminal — the platform's delivery chokepoint
    delivers nothing for it (the run stays parked), and ``visit`` normalises it to the caller/user
    partition of the remaining ids. Generic: it names interaction ids only, no driver
    or engine state. Kept by TYPE through the run-tool seam alongside ``SuspendedInteraction`` so a
    caller recognises a still-suspended step rather than mistaking it for a terminal result.
    """

    remaining_ids: list[str] = Field(default_factory=list)
