"""Hooks-registry settings and the Redis key layout for hooks, triggers, and replay dedupe."""

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control.identity import ReadinessTarget
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import TaiBaseSettings


class HooksRedisSettings(RedisConnectionSettings):
    """Redis connection for the hooks registry, composed from the kit connection shape.

    Connection values come from the ``HOOKS_REDIS_*`` env (``HOOKS_REDIS_URL`` …); with no
    ``redis_url`` the registry runs in-memory.

    With ``HOOKS_REDIS_URL`` set, hook registrations and deliveries live in Redis
    and are shared across every worker. With it unset the registry runs in-memory,
    per-process — valid only for a single worker; siblings do not see each other's
    hooks (see ``HooksSettings``).
    """

    model_config = SettingsConfigDict(env_prefix="HOOKS_")

    # A black-holed Redis fails the hooks-registry op loudly within 5s instead of
    # hanging the request/loop task: the connect phase and each command read are
    # both bounded. Must be positive.
    socket_connect_timeout: float | None = Field(default=5, gt=0)
    socket_timeout: float | None = Field(default=5, gt=0)


class HooksSettings(TaiBaseSettings):
    """Hooks-registry configuration (``HOOKS_*`` env).

    Backend selection follows ``in_memory``: with ``HOOKS_REDIS_URL`` set the
    registry is Redis-backed and shared across all workers; with it unset the
    registry is in-memory and per-process, which is valid ONLY for a single worker
    — sibling workers (and a separate backend worker) do not see each other's
    registrations or deliveries. Set ``HOOKS_REDIS_URL`` for shared state whenever
    more than one worker runs; under ``tai serve`` with more than one worker, each worker
    boot, reload and config write refuses it without the store
    (:func:`~tai42_skeleton.app.boot_rules.require_hooks_store_for_workers`), and a
    registered task backend is refused without it at every boot, reload and config
    write (:func:`~tai42_skeleton.app.boot_rules.require_hooks_store_for_backend`). The
    connection falls back to ``TAI_DEFAULT_REDIS_URL`` when ``HOOKS_REDIS_URL`` is unset.
    """

    model_config = SettingsConfigDict(
        env_prefix="HOOKS_",
        frozen=True,
    )

    # Infra: the redis connection is composed from the kit (a field, not a base),
    # so the feature config declares no connection fields of its own.
    redis: HooksRedisSettings = Field(default_factory=HooksRedisSettings)

    # Bound on the hook-run attempts this process runs at once: the hook-run runtime holds ONE
    # per-process count of slots, read against this value at each admission, so a burst of
    # events and a config apply alike never push the in-flight work past it. Must be
    # positive — there is no unbounded mode.
    max_workers: int = Field(default=10, gt=0)
    prefix: str = "hooks"

    # Hook runs: a run that fails before its hook acts is retried up to ``run_max_attempts``
    # attempts in all, waiting ``run_retry_base_seconds * 2 ** (attempts - 1)`` capped at
    # ``run_retry_cap_seconds`` between them. A running attempt holds a lease of
    # ``run_lease_seconds`` (renewed at a third of it); the sweep claims due and lapsed runs every
    # ``run_sweep_seconds``; shutdown gives the attempts in flight
    # ``run_shutdown_grace_seconds`` before it cancels them.
    run_max_attempts: int = Field(default=5, ge=1)
    run_retry_base_seconds: float = Field(default=10.0, gt=0)
    run_retry_cap_seconds: float = Field(default=600.0, gt=0)
    run_lease_seconds: float = Field(default=60.0, gt=0)
    run_sweep_seconds: float = Field(default=5.0, gt=0)
    run_shutdown_grace_seconds: float = Field(default=20.0, ge=0)

    @model_validator(mode="after")
    def _run_retry_pair(self) -> "HooksSettings":
        if self.run_retry_base_seconds > self.run_retry_cap_seconds:
            raise ValueError("HOOKS_RUN_RETRY_BASE_SECONDS must not exceed HOOKS_RUN_RETRY_CAP_SECONDS")
        return self

    @property
    def in_memory(self) -> bool:
        """Whether the registry runs in-memory (no ``HOOKS_REDIS_URL`` configured)."""
        return not self.redis.redis_url

    def get_hook_key(self, topic: str) -> str:
        """The Redis key holding the hooks registered for ``topic``."""
        # Distinct namespace segment so no topic name can collide with the
        # ``name_trigger_map`` key below.
        return f"{self.prefix}:topic:{topic}"

    @property
    def name_trigger_map_key(self) -> str:
        """The Redis key of the hook-name to trigger map."""
        return f"{self.prefix}:name_trigger_map"

    @property
    def topic_verifiers_key(self) -> str:
        """The Redis hash key of per-topic verifier bindings."""
        # Distinct namespace segment (a hash of topic -> verifier binding JSON) so
        # no topic name can collide with the per-topic hook keys above.
        return f"{self.prefix}:topic_verifiers"

    def webhook_seen_key(self, topic: str, replay_key: str) -> str:
        """The replay seen-set STRING key for a delivery on ``topic``.

        Namespaced by topic so a sender-chosen id (e.g. a shared_secret nonce) reused across topics
        cannot make one topic's delivery mask another's — distinct doors, distinct deliveries.
        """
        return f"{self.prefix}:webhook_seen:{topic}:{replay_key}"

    # -- Hook-run keys ---------------------------------------------------------

    def hook_run_key(self, run_id: str) -> str:
        """The HASH key of one hook-run job."""
        return f"{self.prefix}:run:{run_id}"

    @property
    def hook_runs_due_key(self) -> str:
        """The ZSET of pending and running hook-run ids, scored by the next attempt time or the lease expiry."""
        return f"{self.prefix}:runs:due"

    @property
    def hook_runs_failed_key(self) -> str:
        """The ZSET of failed hook-run ids, scored by the time each failed."""
        return f"{self.prefix}:runs:failed"

    # -- Trigger-link keys ---------------------------------------------------
    #
    # A trigger link is three STRING keys under the shared ``:trigger:`` segment.
    # A record is findable ONLY by its token hash (the raw token is never stored);
    # the name key is the revocation/list index the operator holds once the token
    # is gone; the tombstone is the permanent revocation marker backup import
    # honors. All literal key strings live here (never elsewhere), so the revoke
    # and restore Lua scripts build rec/tomb keys from a hash read IN-SCRIPT by
    # passing these PREFIX-FORM strings as ARGV rather than repeating the literal.

    @property
    def trigger_record_key_prefix(self) -> str:
        """The key prefix for trigger records (looked up by token hash)."""
        return f"{self.prefix}:trigger:rec:"

    @property
    def trigger_name_key_prefix(self) -> str:
        """The key prefix for the trigger name index."""
        return f"{self.prefix}:trigger:name:"

    @property
    def trigger_tomb_key_prefix(self) -> str:
        """The key prefix for trigger revocation tombstones."""
        return f"{self.prefix}:trigger:tomb:"

    def trigger_record_key(self, token_hash: str) -> str:
        """The record STRING key for a token hash — the resolver's lookup key."""
        return f"{self.trigger_record_key_prefix}{token_hash}"

    def trigger_name_key(self, name: str) -> str:
        """The name-index STRING key (value = the token hash) — the revocation and list handle.

        Revoke targets the name's CURRENT hash.
        """
        return f"{self.trigger_name_key_prefix}{name}"

    def trigger_tomb_key(self, token_hash: str) -> str:
        """The permanent revocation tombstone marker key backup import refuses to overwrite with a live record."""
        return f"{self.trigger_tomb_key_prefix}{token_hash}"

    def trigger_name_scan_pattern(self) -> str:
        """The scan pattern matching every trigger name-index key."""
        return f"{self.trigger_name_key_prefix}*"

    def trigger_tomb_scan_pattern(self) -> str:
        """The scan pattern matching every trigger tombstone key."""
        return f"{self.trigger_tomb_key_prefix}*"


def readiness_targets() -> list[ReadinessTarget]:
    """The hooks Redis, unless the hooks store runs in memory."""
    hooks = HooksSettings()
    return [] if hooks.in_memory else [ReadinessTarget("hooks", RedisClient, hooks.redis)]
