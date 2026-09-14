"""The inbound-answer ladder result shapes: outcome, bridge context, and run result."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from tai42_contract.entry_params import validate_entry_params


class InboundAnswerOutcome(StrEnum):
    """What the shared inbound-answer ladder decided for one inbound reply on a
    correlation key. A channel maps this to its own transport ack."""

    NO_CORRELATION = "no_correlation"  # no pending ask on this key — the CALLER bridges it as a normal turn
    FORWARDED = "forwarded"  # the door accepted the answer; the correlation was released
    RETRY_KEPT = "retry_kept"  # the door rejected a re-answerable ask; correlation KEPT, participant re-prompted
    BRIDGED = "bridged"  # the ask is gone or the mismatch is hard; correlation released and the reply bridged
    # A ``bridge``-policy ask rejected a reply: the correlation is KEPT (the ask stays parked) and
    # the reply is bridged as a fresh turn with NO participant notice — the reply was a digression.
    BRIDGED_KEPT = "bridged_kept"


class InboundBridge(BaseModel):
    """The context a bridged turn needs when a reply is not (or no longer) an answer.

    A channel hands one of these to :meth:`AppChannels.handle_inbound_answer` alongside
    the correlation key and answer value. ``channel_id`` is the registered channel
    name; ``our_identity`` and ``client_address`` are the conversation's two addresses
    (the operator identity the turn answers from, and the participant's attested address /
    thread); ``cap_key`` is the party the per-address turn cap holds accountable;
    ``provider_message_id`` dedupes a provider redelivery at the conversation seam;
    ``bridge_text`` is the channel's faithful rendering of the participant's message for a
    bridged turn.

    ``owns_retry_notice`` lets a channel OWN the participant-facing correction message on a
    retryable rejection. The default (False) is that the ladder sends the generic
    "that didn't match, try again" notice on :attr:`InboundAnswerOutcome.RETRY_KEPT`.
    When True, the channel's correction surface IS a re-ask the channel renders off
    RETRY_KEPT (a re-opened WhatsApp Flow, a Slack modal's inline Block-Kit error), so
    the ladder SKIPS its notice to avoid double-messaging — it still keeps the
    correlation and still emits the operator event (tagged ``notice_owner="channel"``).
    It applies ONLY to the retryable path: on a hard mismatch (a closed ask) the
    channel's re-ask surface is moot, so the ladder always sends the final "question is
    closed" notice regardless of this flag.

    ``params`` is the OPTIONAL opaque channel enrichment this inbound reply carries — the
    ANSWER-path counterpart of a conversation entry's ``params``: a tapped reply id, a template
    button payload, a referral, the reply-to context the participant quoted. The ladder threads it BOTH
    ways with the same seam symmetry — forwarded to the ask's callback door alongside the answer
    (landing on :class:`~tai42_contract.interactions.models.InteractionResponse.params`, read by
    the asking flow beside ``answer``) AND, when the reply is instead BRIDGED as a fresh turn,
    passed to ``accept`` as its ``params`` — so enrichment is never dropped on either arm. The
    SAME transport vocabulary (:func:`~tai42_contract.entry_params.validate_entry_params`) bounds
    it; the platform attaches no meaning and NO TRUST. ``None`` means no enrichment.
    """

    model_config = ConfigDict(frozen=True)

    channel_id: str
    our_identity: str
    client_address: str
    cap_key: str
    provider_message_id: str
    bridge_text: str
    owns_retry_notice: bool = False
    params: dict[str, str] | None = None

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return validate_entry_params(value)


class InboundAnswerResult(BaseModel):
    """The result of one inbound-answer ladder run.

    ``outcome`` is the ladder's decision. ``retry_reason`` and ``retry_field`` carry the
    door's OWN (already length-bounded) rejection message and the failing field name so a
    channel that OWNS its correction surface (a re-opened WhatsApp Flow, a Slack modal's
    inline Block-Kit error) can render the door's SPECIFIC message rather than a generic
    line. Both are populated when the door rejected the answer's content — on
    :attr:`InboundAnswerOutcome.RETRY_KEPT` (either ``notice_owner`` variant) and on a
    hard-mismatch :attr:`InboundAnswerOutcome.BRIDGED` — and are ``None`` on every other
    outcome (no correlation, a clean forward, a gone-ask 404 bridge). A channel that
    renders no correction of its own simply ignores them and maps ``outcome``.
    """

    model_config = ConfigDict(frozen=True)

    outcome: InboundAnswerOutcome
    retry_reason: str | None = None
    retry_field: str | None = None
