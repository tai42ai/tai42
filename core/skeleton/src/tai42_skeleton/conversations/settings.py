import hashlib
import json

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings

# Outbound message-length caps a long answer is split against, per channel.
_DEFAULT_MAX_MESSAGE_CHARS: dict[str, int] = {
    "twilio": 1600,
    "telegram": 4096,
    "slack": 40000,
    "whatsapp": 4096,
    # The web chat page has no medium length cap; a large split bound keeps agent
    # answers whole while max_outbound_chunks still caps a runaway fan-out.
    "web": 8000,
}


def _require_key_segment(name: str, value: str) -> None:
    """Raise on a blank key segment: it builds a well-formed key every other blank value
    also builds, silently colliding distinct messages onto one marker/index entry."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string to key a conversation record, got {value!r}")


def _require_qualifier_segment(name: str, value: str) -> None:
    """Raise on a blank or ``:``-bearing key segment that another segment FOLLOWS: a ``:``
    moves the boundary, so ``(a, b:c)`` and ``(a:b, c)`` build the same key. A trailing
    segment needs no such check."""
    _require_key_segment(name, value)
    if ":" in value:
        raise ValueError(f"{name} must not contain ':' — it would collapse into the segment that follows it: {value!r}")


class ConversationsRedisSettings(RedisConnectionSettings):
    """Redis connection for the conversation bridge (``CONVERSATIONS_REDIS_*`` env). With
    no ``redis_url`` there is no durable store and every routing operation refuses."""

    model_config = SettingsConfigDict(env_prefix="CONVERSATIONS_")

    # Seconds; bound connect and each command read so a black-holed Redis fails loudly
    # instead of pinning the request/loop task.
    socket_connect_timeout: float | None = Field(default=5, gt=0)
    socket_timeout: float | None = Field(default=5, gt=0)


class ConversationsSettings(TaiBaseSettings):
    """Conversation-bridge configuration (``CONVERSATIONS_*`` env). Without
    ``CONVERSATIONS_REDIS_URL`` there is no durable store and every routing operation
    refuses with a loud 501."""

    model_config = SettingsConfigDict(
        env_prefix="CONVERSATIONS_",
        frozen=True,
    )

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

    # Per-channel split caps. A routed channel absent from this map is a loud config
    # error at send, never a silent unbounded or truncated send.
    max_message_chars: dict[str, int] = Field(default_factory=lambda: dict(_DEFAULT_MAX_MESSAGE_CHARS))

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
        """Refuse a refresh interval at or above the lease: a live turn's lease would lapse
        between heartbeats and the intake re-drive would reap it."""
        if self.intake_claim_refresh_seconds >= self.intake_claim_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS ({self.intake_claim_refresh_seconds}) must be below "
                f"CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS ({self.intake_claim_lease_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _send_timeout_stays_under_the_delivery_lease(self) -> "ConversationsSettings":
        """Refuse a send timeout at or above the lease: a chunk could then still be in
        flight after the sweep re-claimed the record, and both workers would send it."""
        if self.delivery_send_timeout_seconds >= self.delivery_claim_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_DELIVERY_SEND_TIMEOUT_SECONDS ({self.delivery_send_timeout_seconds}) must be below "
                f"CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS ({self.delivery_claim_lease_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _callback_timeout_stays_under_the_delivery_lease(self) -> "ConversationsSettings":
        """Refuse a callback timeout at or above the lease: a POST could then still be in
        flight after the sweep re-claimed the record, and both workers would POST it."""
        if self.delivery_callback_timeout_seconds >= self.delivery_claim_lease_seconds:
            raise ValueError(
                f"CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS ({self.delivery_callback_timeout_seconds}) must be "
                f"below CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS ({self.delivery_claim_lease_seconds})"
            )
        return self

    @property
    def in_memory(self) -> bool:
        return not self.redis.redis_url

    # -- Keyspace helpers ----------------------------------------------------
    #
    # The twelve conversation keyspaces. Every literal key string lives ONLY here. A
    # provider-supplied id sits LAST in its key and the segment before it is checked
    # ``:``-free, so no provider value can bleed across a segment boundary — the sole
    # exception is ``open_code_key``, whose variable part is a single opaque ``sha256``
    # terminal segment folding charset-unconstrained inputs into a hash, so it carries no
    # qualifier guard.

    def dedupe_key(self, channel: str, provider_message_id: str) -> str:
        """Inbound-dedupe marker key, channel-qualified (a provider message id is unique
        only within its channel). Both halves must be non-blank."""
        _require_qualifier_segment("channel", channel)
        _require_key_segment("provider_message_id", provider_message_id)
        return f"{self.prefix}:dedupe:{channel}:{provider_message_id}"

    def record_key(self, message_id: str) -> str:
        """Answer/delivery record key, keyed by the record's uuid4 ``message_id``."""
        return f"{self.prefix}:record:{message_id}"

    def status_index_key(self, delivery_status: str) -> str:
        """Per-status record index — the sorted set of the ``message_id``s in that state,
        which the re-drive and the sweep read instead of walking the record keyspace. A
        member's score is the moment its row expires (``+inf`` while it carries no TTL)."""
        return f"{self.prefix}:status:{delivery_status}"

    def chunk_ledger_key(self, message_id: str) -> str:
        """Channel send-ledger key — the append-only list of chunks already accepted."""
        return f"{self.prefix}:progress:{message_id}"

    def outbound_index_key(self, channel: str, outbound_message_id: str) -> str:
        """Outbound-id → record reverse-index key, channel-qualified (an outbound provider
        id is unique only within its channel). Both halves must be non-blank."""
        _require_qualifier_segment("channel", channel)
        _require_key_segment("outbound_message_id", outbound_message_id)
        return f"{self.prefix}:outbound:{channel}:{outbound_message_id}"

    def thread_index_key(self, route_name: str, thread_id: str) -> str:
        """Per-thread transcript index — the sorted set of one thread's ``message_id``s,
        scored by ``created_at``, so a transcript read never walks the record keyspace. The
        ``thread_id`` carries ``:`` of its own and so sits LAST."""
        _require_qualifier_segment("route_name", route_name)
        _require_key_segment("thread_id", thread_id)
        return f"{self.prefix}:thread:{route_name}:{thread_id}"

    def route_threads_key(self, route_name: str) -> str:
        """Per-route thread index — the sorted set of the route's ``thread_id``s, scored by
        the moment each thread was last active, so a listing reads the newest first. The
        route name is checked exactly as :meth:`thread_index_key` checks it, so one name can
        never key one of the two thread indexes and be refused by the other."""
        _require_qualifier_segment("route_name", route_name)
        return f"{self.prefix}:route_threads:{route_name}"

    def route_key(self, route_name: str) -> str:
        """Routing-row key for a route name (a ``:``-free slug)."""
        return f"{self.prefix}:route:{route_name}"

    @property
    def route_key_prefix(self) -> str:
        return f"{self.prefix}:route:"

    @property
    def route_names_key(self) -> str:
        """The set index of every stored route name, kept in lockstep with the per-route
        keys by the create/delete scripts."""
        return f"{self.prefix}:route_names"

    # -- Person-linking keyspaces --------------------------------------------

    def person_key(self, person_id: str) -> str:
        """Person-row key (JSON), keyed by the person's uuid4 ``person_id`` (terminal)."""
        _require_key_segment("person_id", person_id)
        return f"{self.prefix}:person:{person_id}"

    @property
    def person_key_prefix(self) -> str:
        """The person-row key prefix the merge/detach scripts build a row key from in-script
        (a survivor id chosen server-side is not known to the caller)."""
        return f"{self.prefix}:person:"

    def person_index_key(self, target_kind: str, target_name: str) -> str:
        """Per-target person index — a HASH ``door_address_key`` → ``person_id``. The
        ``target_name`` is charset-unconstrained and so sits TERMINAL; ``target_kind`` is a
        constrained qualifier checked ``:``-free."""
        _require_qualifier_segment("target_kind", target_kind)
        _require_key_segment("target_name", target_name)
        return f"{self.prefix}:person_index:{target_kind}:{target_name}"

    @property
    def person_index_key_prefix(self) -> str:
        """The person-index key prefix the merge/detach scripts build the index key from
        in-script, once they read the target off the row (``<prefix>:person_index:``, then
        ``<target_kind>:<target_name>`` exactly as :meth:`person_index_key` renders it)."""
        return f"{self.prefix}:person_index:"

    def pair_code_key(self, code_hash: str) -> str:
        """Single-use pair-code record key, keyed by ``sha256(code)`` (hex, terminal). The
        raw code is never a key nor a value anywhere — only its hash is."""
        _require_key_segment("code_hash", code_hash)
        return f"{self.prefix}:pair_code:{code_hash}"

    @property
    def pair_code_key_prefix(self) -> str:
        """The pair-code record key prefix the mint script builds the previously open code's
        key from in-script (it holds only that code's sha256, via the open-code pointer)."""
        return f"{self.prefix}:pair_code:"

    def open_code_key(self, target_kind: str, target_name: str, door_address_key: str) -> str:
        """Open-pair-code pointer key for ONE minting conversation → the sha256 of the
        currently open code. Its variable part is a SINGLE opaque terminal segment — the
        sha256 of the deterministic JSON array ``[target_kind, target_name,
        door_address_key]`` — because ``target_name`` and the address folded into
        ``door_address_key`` are charset-unconstrained (may carry ``:``) and so can never be
        raw non-terminal segments; the hash removes all delimiter ambiguity while keeping an
        ordinary per-key TTL (a per-target HASH could not expire its fields)."""
        _require_key_segment("target_kind", target_kind)
        _require_key_segment("target_name", target_name)
        _require_key_segment("door_address_key", door_address_key)
        opaque = hashlib.sha256(
            json.dumps([target_kind, target_name, door_address_key], separators=(",", ":")).encode()
        ).hexdigest()
        return f"{self.prefix}:open_code:{opaque}"
