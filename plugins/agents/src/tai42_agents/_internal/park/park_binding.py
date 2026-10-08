"""The agents plugin's binding of the kit park index: its namespace, its Redis, and its live-thread filter.

When an async ``ask`` parks a park-capable agent run, the flow-blind platform keeps only the
interaction id. The agents plugin reverses that id to the parked run through the kit's park index
(:mod:`tai42_kit.interactions.park_index`) under the namespace ``agent:park``, in the plugin's OWN
Redis (``TAI_AGENTS_REDIS_URL``, falling back to ``TAI_DEFAULT_REDIS_URL``) — independent of the
checkpoint provider, so a cross-worker resume finds the park even when the paused graph is
checkpointed to postgres. Each entry is the agents' rebuild payload: ``{agent_name, thread_id,
superstep_id, interrupt_id, rebuild_kwargs, completion_tool, completion_context, retention_bound,
execution_identity, execution_fingerprint}``.

The index library imports no Redis client at import time, so the shipped agents import graph stays
free of it; an unconfigured Redis fails loudly at the first read or write.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from tai42_kit.interactions.park_index import ParkIndex

from tai42_agents.settings import AgentsParkRedisSettings, agents_park_redis_settings

AGENTS_PARK_NAMESPACE: Final[str] = "agent:park"

_bound: tuple[AgentsParkRedisSettings, ParkIndex] | None = None


def agents_park_index() -> ParkIndex:
    """The agents' park index over the current park Redis settings (rebuilt when a settings reload replaces them)."""
    global _bound
    settings = agents_park_redis_settings()
    if _bound is None or _bound[0] is not settings:
        _bound = (settings, ParkIndex(AGENTS_PARK_NAMESPACE, settings))
    return _bound[1]


def park_index_configured() -> bool:
    """Whether the agents plugin's durable park index has a Redis to reach.

    The index is an OPTIONAL feature dependency: a deployment that never async-parks configures no
    ``TAI_AGENTS_REDIS_URL`` and owns no parks. The callers that must tolerate an unconfigured index
    gate on this first — the park-capability gate (so an async ask refuses cleanly rather than
    half-parking), the live-thread filter, and the globally registered kill and give-up handlers
    (which fire for EVERY driver's park, so a deployment with the plugin loaded but no park Redis
    is not crashed by a park it does not own). A CONFIGURED index that then fails a read raises.
    """
    return agents_park_redis_settings().redis_url is not None


async def threads_with_live_barriers(provider: str, conn_string: str | None, thread_ids: Sequence[str]) -> set[str]:
    """The agents live-thread filter: the subset of ``thread_ids`` with at least one live park barrier.

    Empty when no park index is configured (no park could have been persisted). The park index
    names the thread whatever checkpoint store holds it, so ``provider`` and ``conn_string`` are
    not read.
    """
    if not park_index_configured():
        return set()
    return await agents_park_index().threads_with_live_barriers(thread_ids)
