"""The ``ask`` answer and interaction-state models.

``InteractionResponse`` is the validated answer pushed onto the reply channel;
``AskAnswer`` is the one value a synchronous asker receives;
``InteractionState`` is the mutable record the answer endpoint reads;
``SuspendedInteraction`` is the sentinel an ``async`` ask returns in place of an answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.entry_params import validate_entry_params
from tai42_contract.interactions.models.request import InteractionRequest
from tai42_contract.interactions.reply_text import ReplyText
from tai42_contract.preset_stages import PresetOutputStage
from tai42_contract.reply_to import ReplyTo

#: The MCP ``_meta`` key under which the platform's elicitation bridge hands an elicitation asker
#: the message the answering reply quoted: ``ReplyTo.model_dump(mode="json")``, or ``null`` when the
#: reply quoted none or the answer did not come over a channel. The bridge asks an elicitation with no
#: channel, so it is answered in the inbox and the key reads ``null`` there. Parsed back with
#: :meth:`~tai42_contract.conversations.ReplyTo.model_validate`.
ELICIT_META_REPLY_TO: Final = "tai42/reply_to"


class InteractionResponse(BaseModel):
    """The answer. Validated server-side before it wakes the caller.

    ``params`` is the OPTIONAL opaque channel enrichment carried WITH the answer — the answer-path
    counterpart of the bridge path's :class:`~tai42_contract.channels.InboundBridge.params` and a
    conversation entry's ``params``: when a tap/reply that ANSWERS a pending ask also carries
    channel-specific context (a tapped reply id, a template button payload, a referral) the
    channel encodes it as string entries here. The SAME transport vocabulary
    (:func:`~tai42_contract.entry_params.validate_entry_params`) bounds it as every params seam;
    the platform attaches no meaning and NO TRUST. ``None`` means no enrichment — a plain answer.
    The asker receives it as is, beside ``answer``, in the :class:`AskAnswer` a synchronous ask
    returns and, as JSON, in a continuation's ``ask_result`` (as a preset's output stage maps it,
    when the park sits under one).

    ``medium_message_id`` is the id the medium gave the participant's reply message, the one a
    later quote names; ``None`` when the answer did not arrive as a message on a medium (an
    operator or caller answer) or the medium has no such id.

    ``reply_to_medium_message_id`` is the medium's id of the message the answering reply quoted;
    ``None`` when it quoted none or the answer did not arrive over a channel. It holds the id only,
    never the quoted text: the platform resolves the text from the conversation record and hands
    it to the asker as ``reply_to``, beside ``answer``.
    """

    interaction_id: str
    answer: Any
    answered_by: str
    answered_at: datetime
    params: dict[str, str] | None = None
    medium_message_id: str | None = None
    reply_to_medium_message_id: str | None = None

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return validate_entry_params(value)


class AskAnswer(BaseModel):
    """The one value a synchronous asker receives: the answer, the message it quoted, and its params.

    A parked ask's continuation is handed the same value as JSON, its ``ask_result``; on expiry its
    ``answer`` is the expiry marker and ``reply_to`` and ``params`` are ``None``.

    ``answer`` is the answer value exactly as the answering party gave it; on a sensitive ask it is
    the :class:`~tai42_contract.secrets.SecretValue` wrapping it, held by reference, so dumping this
    value to JSON refuses loudly as a dump of the bare ``SecretValue`` does. The answer is never
    changed or merged with the quote or the params.

    ``reply_to`` is the message the answering reply quoted, as the platform resolved it from the
    conversation record; ``None`` when the reply quoted none or the answer did not come over a
    channel. ``params`` is the channel's opaque enrichment of the answering tap or reply
    (:attr:`InteractionResponse.params`, the same vocabulary, no platform meaning), passed through
    as is; ``None`` when the answer carried none. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: Any
    reply_to: ReplyTo | None
    params: dict[str, str] | None

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
    # The park's deadline. For one ask, that ask's deadline; a driver that surfaces a whole
    # super-step reports the LATEST of its open asks' deadlines — the moment past which nothing
    # of the step remains open — so a caller that parks on the step, releases it, or sizes any
    # wait for it bounds that wait by this value, the same rule the step's own barrier is sized
    # by. ``None`` when no open ask carries a deadline.
    expiry_at: datetime | None = None
    # The resume continuation this park was raised UNDER — the one driver entitled to ADOPT
    # it as its own park (see ``tai42_kit.interactions.park_adoption.assert_park_adoptable``). A
    # park has exactly one resume owner, so a caller that turns a returned sentinel into its own
    # park state must be that owner.
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
    # The stages of the preset dispatches this park passed through that the adopting driver applies
    # when the nested run's result reaches it, innermost first. Excluded from every dump, so it
    # never rides the marker, a log or a stored record; it exists only on the in-process object.
    output_stages: list[PresetOutputStage] = Field(default_factory=list[PresetOutputStage], exclude=True)

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


class RunFailed(BaseModel):
    """A run's failed terminal returned across a chain fire; ``outcome`` is the failing driver's opaque mapping.

    A driver that resumes another driver's run through a chain-delivery tool reads the failure by
    TYPE, never by a status word inside the outcome: the outcome stays the failing driver's own
    JSON mapping, carried whole. A continuation face raises
    :class:`~tai42_contract.interactions.RunTerminalFailed` with it, so the platform delivers FAILED.

    ``reply`` is the optional participant-facing text a conversation door shows in place of its
    fixed failure line, under the same rules as :class:`~tai42_contract.interactions.RunTerminalFailed`'s
    ``reply``: the platform never inspects or changes it, and the target that sets it owns that it
    is safe for the participant. ``None`` means no reply.
    """

    model_config = ConfigDict(frozen=True)

    outcome: dict[str, Any]
    reply: ReplyText | None = None
