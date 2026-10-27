"""Conversation-bridge settings: the bridge's Redis connection, its limits and leases, and its key prefix."""

from typing import ClassVar

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control.identity import ReadinessTarget
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import TaiBaseSettings, settings_cache

from tai42_skeleton.conversations.keyspace import ConversationsKeyspace


class ConversationsRedisSettings(RedisConnectionSettings):
    """Redis connection for the conversation bridge (``CONVERSATIONS_REDIS_*`` env).

    With no ``redis_url`` there is no durable store and every routing operation refuses.
    """

    model_config = SettingsConfigDict(env_prefix="CONVERSATIONS_")

    # Seconds; bound connect and each command read so a black-holed Redis fails loudly
    # instead of pinning the request/loop task.
    socket_connect_timeout: float | None = Field(default=5, gt=0)
    socket_timeout: float | None = Field(default=5, gt=0)


class ConversationsSettings(TaiBaseSettings):
    """Conversation-bridge configuration (``CONVERSATIONS_*`` env).

    Without ``CONVERSATIONS_REDIS_URL`` there is no durable store and every routing operation refuses
    with a loud 501. The ``CONVERSATIONS_`` prefix is owned: an env name under it that no
    conversations setting declares is refused.
    """

    model_config = SettingsConfigDict(
        env_prefix="CONVERSATIONS_",
        frozen=True,
    )
    env_prefix_owned: ClassVar[bool] = True

    redis: ConversationsRedisSettings = Field(default_factory=ConversationsRedisSettings)

    prefix: str = "conversations"

    # -- Turn-engine bounds --------------------------------------------------

    # ONE semaphore of this size bounds TOTAL in-flight turns on the worker.
    max_concurrent_turns: int = Field(default=10, gt=0)

    # Per-thread FIFO depth; the message that would exceed it is refused loudly.
    thread_queue_depth: int = Field(default=20, gt=0)

    # Per-``client_address`` token-bucket rate (turns per rolling hour).
    per_address_turns_per_hour: int = Field(default=20, gt=0)

    # Max per-address token buckets one worker holds; a flood of new addresses evicts
    # rather than growing the map without limit.
    address_bucket_max_entries: int = Field(default=50_000, gt=0)

    # Seconds; upper clamp on the API door's optional sync wait before it falls back to
    # the 202 + callback path.
    sync_wait_max_seconds: int = Field(default=120, gt=0)

    # Seconds a running turn's intake lease stays live. The turn refreshes it as it runs;
    # only a lapsed lease may be adopted and failed by the intake re-drive.
    intake_claim_lease_seconds: int = Field(default=120, gt=0)

    # Seconds between intake-lease refreshes; must stay under the lease.
    intake_claim_refresh_seconds: int = Field(default=30, gt=0)

    # Seconds a running turn's cross-worker thread lease stays live. The turn refreshes it as
    # it runs; only on expiry (a crashed holder) may another worker take the thread.
    thread_lease_seconds: int = Field(default=120, gt=0)

    # Seconds between thread-lease refreshes; must stay under the lease.
    thread_lease_refresh_seconds: int = Field(default=30, gt=0)

    # Seconds the overlap-cancel watcher sleeps between reads of a thread's cancel marker while a
    # ``running="cancel"`` turn runs. Must stay under the thread lease, or the marker naming a newer
    # message could expire before the watcher next reads it and the running turn is never cancelled.
    overlap_cancel_poll_seconds: float = Field(default=0.5, gt=0)

    # Seconds a LIVE-CALLER sync door (operator send, thread/person delete) waits to acquire a
    # thread's turn slot before refusing with a loud 503; a background turn stays unbounded.
    sync_door_wait_seconds: float = Field(default=30, gt=0)

    # Seconds; the hard ceiling on ONE turn's working-on-it (typing) refresh loop. The loop
    # normally stops at the first outbound send, but a turn that never sends is bounded here so a
    # hung turn's indicator never refreshes unbounded. 0 disables the loop entirely (the kill switch).
    working_signal_max_seconds: float = Field(default=180.0, ge=0)

    # Seconds subtracted from a channel's vendor-indicator lifetime to pick the refresh interval,
    # so each refresh lands before the indicator lapses. Clamped to half the lifetime when the
    # margin would meet or exceed it, keeping the interval positive for a short-lived indicator.
    working_signal_refresh_margin_seconds: float = Field(default=5.0, gt=0)

    # -- Delivery bounds -----------------------------------------------------

    # Delivery attempts before an undelivered answer is marked ``failed`` (loud, retained).
    delivery_max_attempts: int = Field(default=8, gt=0)

    # Seconds; the first retry waits ``base`` and each later one doubles, capped at ``max``.
    delivery_backoff_base_seconds: float = Field(default=60, gt=0)
    delivery_backoff_max_seconds: float = Field(default=900, gt=0)

    # Seconds a delivery worker's exactly-once claim on a record stays live. The holder
    # refreshes it as it progresses; only on expiry may the sweep reclaim the record.
    delivery_claim_lease_seconds: int = Field(default=120, gt=0)

    # Seconds bounding ONE outbound channel send. Must stay under the lease, or a send can
    # outlive the claim covering it and a second worker sends the same chunk.
    delivery_send_timeout_seconds: float = Field(default=60, gt=0)

    # Seconds bounding ONE api-door callback POST. Must stay under the lease, or a POST can
    # outlive the claim covering it and a second worker re-POSTs the same signed callback.
    delivery_callback_timeout_seconds: float = Field(default=15, gt=0)

    # Seconds between stalled-delivery sweep passes.
    delivery_sweep_interval_seconds: int = Field(default=60, gt=0)

    # Seconds a ``provisional`` record waits for an out-of-band receipt before it is
    # confirmed ``delivered`` on expiry.
    delivery_grace_seconds: int = Field(default=3600, gt=0)

    # OPERATOR OVERRIDE of the per-channel split cap, keyed by channel name. Empty by default:
    # each channel declares its OWN medium cap through the ``max_message_chars`` seam (read off the
    # channel at send). An entry here wins over the channel's declared cap for that channel; a
    # routed channel with neither an override here nor a declared cap is a loud config error at
    # send, never a silent unbounded or truncated send.
    max_message_chars: dict[str, int] = Field(default_factory=dict)

    # Max provider messages ONE inbound answer may fan out to. An answer that splits past
    # this is refused with a client-safe error reply, never silently fanned out or truncated.
    max_outbound_chunks: int = Field(default=10, gt=0)

    # -- Retention / idempotency TTLs ----------------------------------------

    # Seconds a seen ``(channel, provider_message_id)`` stays deduped.
    inbound_dedupe_ttl_seconds: int = Field(default=48 * 3600, gt=0)

    # Seconds; applied to a record and its reverse index ONLY on the terminal transition —
    # an intake/pending/provisional record carries no expiry until then.
    answer_retention_ttl_seconds: int = Field(default=30 * 86400, gt=0)

    # Seconds a minted pair code (and its open-pointer) stays live before it expires
    # unredeemed. The raw code exists only in flight; only its sha256 is ever stored.
    pair_code_ttl_seconds: int = Field(default=900, gt=0)

    # Seconds an owed first-contact greeting that carries NO pairing code stays parked for its
    # thread's first delivering message turn to consume. A greeting WITH a code is bounded by the
    # pair-code lifetime instead (a dead code must never be presented); a code-less greeting has
    # nothing that expires, so it takes this (longer) horizon rather than the pair-code window.
    owed_greeting_ttl_seconds: int = Field(default=86400, gt=0)

    # -- Redeem brute-force backoff ------------------------------------------
    #
    # A failures-only throttle on the pair-code REDEEM path (per target + the door's
    # accountable party): a valid redeem clears the counter, so an honest user is never
    # blocked, while a source that keeps submitting INVALID codes is locked out with capped
    # exponential backoff — a throttled attempt returns the SAME uniform invalid-code
    # reply (no oracle). The defaults allow a few honest fat-finger retries before the
    # backoff begins and cap the lock at the pair-code TTL; both are deployment-tunable.

    # Consecutive INVALID redeems from one (target, source) before the backoff engages.
    redeem_backoff_threshold: int = Field(default=5, gt=0)

    # Seconds; the exponential per-source redeem lock is capped here, and the failure
    # counter decays after this long of inactivity so a source is never escalated forever.
    redeem_backoff_cap_seconds: int = Field(default=900, gt=0)

    @field_validator("max_message_chars")
    @classmethod
    def _split_caps_are_positive(cls, value: dict[str, int]) -> dict[str, int]:
        """Refuse a non-positive cap at startup: a send can never be split against one."""
        invalid = {name: cap for name, cap in value.items() if cap <= 0}
        if invalid:
            raise ValueError(f"CONVERSATIONS_MAX_MESSAGE_CHARS entries must be positive, got {invalid}")
        return value

    @model_validator(mode="after")
    def _refresh_stays_under_the_intake_lease(self) -> "ConversationsSettings":
        """Refuse a refresh interval at or above the intake lease.

        A live turn's lease would lapse between heartbeats and the intake re-drive would reap it.
        """
        if self.intake_claim_refresh_seconds >= self.intake_claim_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS ({self.intake_claim_refresh_seconds}) must be below "
                f"CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS ({self.intake_claim_lease_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _refresh_stays_under_the_thread_lease(self) -> "ConversationsSettings":
        """Refuse a refresh interval at or above the thread lease.

        A live turn's lease would lapse between heartbeats and another worker would fork its checkpoint.
        """
        if self.thread_lease_refresh_seconds >= self.thread_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_THREAD_LEASE_REFRESH_SECONDS ({self.thread_lease_refresh_seconds}) must be below "
                f"CONVERSATIONS_THREAD_LEASE_SECONDS ({self.thread_lease_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _cancel_poll_stays_under_the_thread_lease(self) -> "ConversationsSettings":
        """Refuse an overlap-cancel poll interval at or above the thread lease.

        The cancel marker carries the thread lease as its TTL, so a poll interval that long could let
        the marker naming a newer message expire before the watcher next reads it, leaving a
        ``running="cancel"`` turn that should have been cancelled running to completion.
        """
        if self.overlap_cancel_poll_seconds >= self.thread_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS ({self.overlap_cancel_poll_seconds}) must be below "
                f"CONVERSATIONS_THREAD_LEASE_SECONDS ({self.thread_lease_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _send_timeout_stays_under_the_delivery_lease(self) -> "ConversationsSettings":
        """Refuse a send timeout at or above the delivery lease.

        A chunk could then still be in flight after the sweep re-claimed the record, and both workers
        would send it.
        """
        if self.delivery_send_timeout_seconds >= self.delivery_claim_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_DELIVERY_SEND_TIMEOUT_SECONDS ({self.delivery_send_timeout_seconds}) must be below "
                f"CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS ({self.delivery_claim_lease_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _callback_timeout_stays_under_the_delivery_lease(self) -> "ConversationsSettings":
        """Refuse a callback timeout at or above the delivery lease.

        A POST could then still be in flight after the sweep re-claimed the record, and both workers
        would POST it.
        """
        if self.delivery_callback_timeout_seconds >= self.delivery_claim_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS ({self.delivery_callback_timeout_seconds}) must be "
                f"below CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS ({self.delivery_claim_lease_seconds})"
            )
        return self

    @property
    def in_memory(self) -> bool:
        """Whether the bridge runs without a durable store (no ``redis_url`` configured)."""
        return not self.redis.redis_url

    @property
    def keyspace(self) -> ConversationsKeyspace:
        """The Redis keys the bridge reads and writes, under this :attr:`prefix`."""
        return ConversationsKeyspace(self.prefix)


@settings_cache
def conversations_settings() -> ConversationsSettings:
    """The cached :class:`ConversationsSettings` for this process, re-read on every settings reset."""
    return ConversationsSettings()


def readiness_targets() -> list[ReadinessTarget]:
    """The conversation-bridge Redis, unless the bridge runs without a durable store."""
    conversations = conversations_settings()
    return [] if conversations.in_memory else [ReadinessTarget("conversations", RedisClient, conversations.redis)]
