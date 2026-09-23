"""The one-pending-per-address parked-ask record (``Correlation``) and its store port."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from tai42_contract.interactions.models import AnswerMismatchPolicy


class Correlation(BaseModel):
    """The per-address record a channel keeps while ONE parked ask awaits a reply.

    When ``ask`` is delivered on a medium whose reply arrives as a fresh
    inbound message (not a tap on a signed link), the channel stores this record
    against a channel-computed correlation key and, when the participant's next reply
    lands on that key, forwards it to ``callback_url`` (the delivery's public
    answer sink). ``interaction_id`` identifies the parked ask (carried into
    operator alerts, never re-derived); ``ttl_deadline`` is the tz-aware instant
    past which the pending ask is stale and the key may be reclaimed. One pending
    ask per address: a channel reserves the key before delivering and drops it
    once the reply is forwarded, withdrawn or expired.
    """

    model_config = ConfigDict(frozen=True)

    callback_url: str  # the delivery's public /api/interactions/callback/{ticket} answer sink
    interaction_id: str  # the parked ask this record awaits a reply for
    ttl_deadline: datetime  # tz-aware; past it the pending ask is stale and the key reclaimable
    # The parked ask's digression policy the shared answer ladder reads at a 400 rejection:
    # ``retry`` (keep + notify, today's behavior) or ``bridge`` (keep + bridge the reply as a
    # fresh turn, no notice). A channel copies it from the ``ChannelDelivery`` it delivered;
    # a channel that does not set it keeps the safe default.
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY
    # The ask's custom retry-notice text the ladder sends instead of the built-in one (``retry``
    # policy only); ``None`` uses the built-in notice. Carried from the ``ChannelDelivery``.
    mismatch_notice: str | None = None

    @field_validator("ttl_deadline")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # A naive deadline compared against an aware ``now()`` raises TypeError at
        # use time; reject it here and normalize to UTC (same strictness as
        # ChannelDelivery.timeout_at).
        if value.tzinfo is None:
            raise ValueError("ttl_deadline must be timezone-aware (UTC)")
        return value.astimezone(UTC)


@runtime_checkable
class CorrelationStore(Protocol):
    """Storage primitives ONLY for the one-pending-per-address correlation record — no policy.

    A channel that delivers ``ask`` questions whose replies arrive as fresh
    inbound messages keeps a :class:`Correlation` per waiting address so the
    participant's next reply resolves the right parked ask. This port is the minimal
    set/get/release surface over that store; the LADDER that interprets a
    forwarded answer's outcome (forward, retry-in-place, bridge) lives in core and
    reads this port — it is not the store's concern.

    ``key`` is an OPAQUE channel-computed correlation key: it absorbs the divergent
    per-channel shapes — a Twilio number pair, a Slack ``thread_ts``, a Telegram
    ForceReply ``message_id``, a WhatsApp address — collapsing them to one string
    the store never interprets. A channel with no correlated replies (a link-tap
    or an external-answer medium) simply does not provide a store.

    This is a STANDALONE optional port, NOT an extension of :class:`Channel`: a
    channel implements it separately (or not at all), and the core handler is
    handed one explicitly rather than reaching it off the channel instance.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` with a ``ttl_seconds`` expiry, NX.

        Returns True when the key was free and is now held; False when the key is
        already held — the one-pending-per-address guarantee, so a second parked
        ask for the same address never silently overwrites the first. ``ttl_seconds``
        bounds how long the reservation survives without a reply.
        """
        ...

    async def get_correlation(self, key: str) -> Correlation | None:
        """Return the record held under ``key``, or ``None`` when none is held.

        A non-destructive peek: it neither drops nor refreshes the reservation, so
        the handler can inspect the pending ask and decide the outcome before
        releasing.
        """
        ...

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation held under ``key``, idempotently.

        A no-op when the key is already free (expired, forwarded, or never held),
        so releasing twice — or racing an expiry — is never an error.
        """
        ...
