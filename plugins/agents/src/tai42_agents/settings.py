"""``TAI_AGENTS_*`` limits shared by the agents in this package.

The concrete ``TaiBaseSettings`` subclass self-registers with the settings
registry, so a live-reload reset re-reads the env with no extra wiring. Every
field carries a positive safe default — never hardcoded at a use site, never
unlimited.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class AgentsParkRedisSettings(RedisConnectionSettings):
    """The agents plugin's OWN durable Redis, holding the async-park index that
    reverses a parked interaction id back to its parked agent run.

    Independent of the checkpoint provider (a park checkpointed to postgres still
    needs a durable, cross-worker index to find it), and independent of the
    interactions store. Connection values read from ``TAI_AGENTS_REDIS_*``
    (``TAI_AGENTS_REDIS_URL`` ...), or the shared ``TAI_DEFAULT_REDIS_URL``; absent
    means no durable park index, so a park-capable run is refused loudly rather than
    parked into a store that cannot record it."""

    model_config = SettingsConfigDict(env_prefix="TAI_AGENTS_")

    redis_url: str | None = None
    redis_max_connections: int | None = 10


@settings_cache
def agents_park_redis_settings() -> AgentsParkRedisSettings:
    return AgentsParkRedisSettings()


class AgentsLimitsSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAI_AGENTS_")

    # Hard ceiling on the voters list a single voting call may fan out to; an
    # over-limit call raises loudly before any voter LLM runs. Must be positive.
    max_voters: int = Field(default=16, gt=0)

    # How many voters run concurrently within one voting call; the rest queue
    # behind a semaphore. Bounds the parallel LLM cost of one call. Must be
    # positive.
    voter_concurrency: int = Field(default=8, gt=0)

    # Bound on the retrieval agent's embedding-dims probe cache (an LRU); the
    # oldest entry is evicted past this. Must be positive.
    embedding_dims_cache_size: int = Field(default=64, gt=0)

    # Default LangGraph ``recursion_limit`` applied when a run pins none. Caps the
    # super-steps one turn's top-level graph may take (a tools-agent cycle is 2
    # super-steps), bounding paid model calls on a runaway loop. A caller-supplied
    # limit wins. Bounds the top-level graph only. Must be positive.
    default_recursion_limit: int = Field(default=50, gt=0)


@settings_cache
def agents_limits_settings() -> AgentsLimitsSettings:
    return AgentsLimitsSettings()
