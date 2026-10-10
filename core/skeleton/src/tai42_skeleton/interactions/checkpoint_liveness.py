"""The interactions live-thread filter: a checkpoint thread backing a live async park is live.

Registered with the kit's live-thread filter registry under ``interactions`` when this module is
imported, so the checkpoint sweep never deletes a thread a parked run still needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.llm.checkpoint import register_live_thread_filter

from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore

if TYPE_CHECKING:
    from collections.abc import Sequence


async def threads_with_live_parks(provider: str, conn_string: str | None, thread_ids: Sequence[str]) -> set[str]:
    """The subset of ``thread_ids`` that still back at least one live async park.

    Reads the interactions store's thread reverse index UNION the thread subject index — the
    platform's authoritative reach of a thread's live parks, the SAME reach a thread delete kills
    through (:func:`~tai42_skeleton.interactions.helper.cancel_parks_for_thread`). A terminal exit
    removes a park's member, so a member present here is a park still waiting for an answer or its
    expiry. Empty when the interactions store is unconfigured — no park could ever have been
    persisted. The checkpoint store does not change the answer: a park index entry names the thread.
    """
    if not interactions_settings().redis.redis_url:
        return set()
    store = InteractionStore(interactions_settings().key_prefix)
    parked: set[str] = set()
    async with client_ctx(RedisClient, interactions_settings().redis) as conn:
        for thread_id in thread_ids:
            if await store.thread_park_members(conn, thread_id) or await store.subject_members(
                conn, "thread", thread_id
            ):
                parked.add(thread_id)
    return parked


register_live_thread_filter("interactions", threads_with_live_parks)
