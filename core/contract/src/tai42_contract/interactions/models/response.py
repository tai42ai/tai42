"""The ``ask_user`` answer and interaction-state models.

``InteractionResponse`` is the validated answer pushed onto the reply channel;
``InteractionState`` is the mutable record the answer endpoint reads;
``SuspendedInteraction`` is the sentinel an ``async`` ask returns in place of an answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

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
