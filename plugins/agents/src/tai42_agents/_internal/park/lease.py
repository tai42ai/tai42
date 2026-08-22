"""The shared cross-worker per-workspace drive lease.

Both durable-workspace engines (``claude_code`` and ``langchain_deep_agent``) serialize the
turns of one ``thread_id`` ACROSS WORKERS on this one Redis lease, so two workers never open
two sandbox sessions on the same workspace volume at once (the volume is NOT idempotent under
concurrent drives — two sessions writing it corrupt each other). Written ONCE here over the
same ``client_ctx(RedisClient, agents_park_redis_settings())`` the park index uses, never
hand-copied per engine.

Discipline (deliberate, stated):

* POSITIVE ACK — ``SET NX`` returns set/not-set, so a held lease is a loud constant-message
  busy error, never a silent block.
* LONG-LEASE, NO HEARTBEAT — a heartbeated short lease expiring under a live drive is itself a
  corruption vector (a second worker would win and open a second session), so the lease is
  sized to EXCEED one drive's max wall-clock and never renewed. A crash is bounded by
  ``lease_ms``.
* NORMAL release is the turn-end ``finally`` compare-and-delete (token-checked, so a lapsed
  holder never drops a reclaimer's fresh lease).

Deliberately NOT an in-container ``flock``: a session's fd lives inside the container, so a
worker crash would not free it, and a blocking flock gives no positive acquire ACK.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final, cast

from tai42_agents._internal.park.errors import WorkspaceLeaseHeldError
from tai42_agents.settings import agents_park_redis_settings

# The engine-neutral key prefix for the per-workspace mutex, matching this package's existing
# ``agent:park:`` convention. Both engines lock the SAME key for a given ``workspace_key``.
WSLOCK_KEY_PREFIX: Final[str] = "agent:park:wslock:"

# Headroom (seconds) an engine adds over its run timeout when sizing ``lease_ms``, covering the
# non-drive volume work a turn does under the lease (skill copy, payload authoring, teardown
# scrub) so the lease never expires while that work is still mutating the volume.
LEASE_HEADROOM_SECONDS: Final[int] = 120

# Compare-and-delete the lease in ONE round trip: the DEL fires only while the stored token
# still matches this holder, so a lapsed holder's release can never drop a reclaimer's lease.
_RELEASE_LEASE_SCRIPT: Final[str] = """
-- agent:park:wslock-release
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _wslock_key(workspace_key: str) -> str:
    return f"{WSLOCK_KEY_PREFIX}{workspace_key}"


@contextlib.asynccontextmanager
async def _lease_client() -> AsyncIterator[Any]:
    # Redis is an OPTIONAL feature dependency reached lazily (as the park index does), so the
    # shipped import graph stays a light leaf and an unconfigured/uninstalled redis surfaces
    # loudly at the first lease acquire, never silently.
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    async with client_ctx(RedisClient, agents_park_redis_settings()) as client:
        yield client


@contextlib.asynccontextmanager
async def workspace_lease(workspace_key: str, *, lease_ms: int) -> AsyncIterator[None]:
    """Hold the per-workspace drive lease for the body's duration, or raise
    :class:`WorkspaceLeaseHeldError` when another worker already holds it.

    ``SET <token> NX PX lease_ms`` grants the lease to exactly one caller. The body runs
    while held; the ``finally`` releases it via the token-checked compare-and-delete, so a
    normal turn frees it at once and a crash lets it expire after ``lease_ms``. ``lease_ms``
    is sized by the engine to exceed one drive's max wall-clock plus its volume-cleanup
    headroom, so the lease never expires under a live drive."""
    key = _wslock_key(workspace_key)
    token = str(uuid.uuid4())
    async with _lease_client() as client:
        acquired = await client.set(key, token, nx=True, px=lease_ms)
        if not acquired:
            raise WorkspaceLeaseHeldError(workspace_key)
        try:
            yield
        finally:
            await cast("Any", client.eval(_RELEASE_LEASE_SCRIPT, 1, key, token))
