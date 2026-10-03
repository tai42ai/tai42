"""Checkpoint retention — the sweep that expires idle conversation threads.

``sweep_checkpoints`` deletes every thread whose newest checkpoint is older than
``checkpoint_ttl_minutes`` (the kit ``LLMProviderSettings``). It is the retention
mechanism for the DB-backed providers (``postgres``/``sqlite``); ``redis`` carries
its own native key TTL and ``memory`` is process-lifetime, so both are a no-op
here, as is an unset TTL. Deletion uses the saver's own ``adelete_thread`` surface.

As an operation it projects as a tool, so it is runnable by name through
``/api/schedules``. Native recurrence additionally needs a ``schedule_task``-branched
vehicle (a deployment manifest wraps the tool with the backend's schedule extension);
external cron of ``tai checkpoints sweep`` is the busless alternative.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.settings import llm_provider_settings

from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.operations import operation
from tai42_skeleton.operations.response_models_group_c import CheckpointSweepResult

# Providers with a persisted store the sweep walks; redis uses a native key TTL, memory is process-lifetime.
_SWEEPABLE_PROVIDERS = frozenset({"postgres", "sqlite"})


async def _threads_with_live_parks(thread_ids: list[str]) -> set[str]:
    """The subset of ``thread_ids`` that still back at least one live async park.

    Reads the interactions store's thread reverse index UNION the thread subject index — the
    platform's authoritative reach of a thread's live parks, the SAME reach a thread delete kills
    through (:func:`~tai42_skeleton.interactions.helper.cancel_parks_for_thread`). A terminal exit
    removes a park's member, so a member present here is a park still waiting for an answer or its
    expiry. Empty when the interactions store is unconfigured — no park could ever have been
    persisted.
    """
    settings = interactions_settings()
    if not settings.redis.redis_url:
        return set()
    store = InteractionStore(settings.key_prefix)
    parked: set[str] = set()
    async with client_ctx(RedisClient, settings.redis) as conn:
        for thread_id in thread_ids:
            if await store.thread_park_members(conn, thread_id) or await store.subject_members(
                conn, "thread", thread_id
            ):
                parked.add(thread_id)
    return parked


@operation(
    summary="Sweep expired conversation checkpoints",
    tags=["checkpoints"],
    destructive=True,
    reload_gated=True,
    response_model=CheckpointSweepResult,
)
async def sweep_checkpoints() -> dict[str, Any]:
    """Delete conversation threads whose newest checkpoint is older than the configured idle lifetime.

    Returns the provider, the TTL, and the swept threads.
    A no-op (nothing deleted) when the TTL is unset, or the provider is ``redis``
    (native key TTL) or ``memory`` (process-lifetime) — each reported in ``skipped``.
    """
    settings = llm_provider_settings()
    provider = settings.checkpoint
    ttl_minutes = settings.checkpoint_ttl_minutes

    if provider not in _SWEEPABLE_PROVIDERS:
        return {
            "provider": provider,
            "ttl_minutes": ttl_minutes,
            "swept_count": 0,
            "swept_threads": [],
            "skipped": f"provider {provider!r} has no swept store (redis uses a key TTL; memory is process-lifetime)",
        }

    if ttl_minutes is None:
        return {
            "provider": provider,
            "ttl_minutes": None,
            "swept_count": 0,
            "swept_threads": [],
            "skipped": "retention disabled (checkpoint_ttl_minutes unset); checkpoints are kept forever",
        }

    saver = await checkpoint_registry().get_checkpointer(provider=provider, conn_string=settings.checkpoint_conn_string)
    cutoff = datetime.now(UTC) - timedelta(minutes=ttl_minutes)

    # Staleness = each thread's newest checkpoint timestamp vs the cutoff.
    newest_by_thread: dict[str, datetime] = {}
    async for tup in saver.alist(None):
        configurable = tup.config.get("configurable") or {}
        thread_id = configurable["thread_id"]
        ts = datetime.fromisoformat(tup.checkpoint["ts"])
        current = newest_by_thread.get(thread_id)
        if current is None or ts > current:
            newest_by_thread[thread_id] = ts

    stale = sorted(thread_id for thread_id, ts in newest_by_thread.items() if ts < cutoff)
    # A thread backing a still-live async park is NOT idle: deleting its checkpoints would destroy
    # a run that is parked and waiting for an answer or its expiry. Keep every such thread — the
    # idle-staleness heuristic cannot see that a parked run is alive, so the live-park index is
    # consulted before any delete.
    parked = await _threads_with_live_parks(stale)
    swept = [thread_id for thread_id in stale if thread_id not in parked]
    for thread_id in swept:
        await saver.adelete_thread(thread_id)

    return {
        "provider": provider,
        "ttl_minutes": ttl_minutes,
        "swept_count": len(swept),
        "swept_threads": swept,
    }
