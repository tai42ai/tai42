"""The Redis-backed tool-run store: every key shape and the record read/write operations."""

from __future__ import annotations

import json
from typing import Any

from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

from .models import _RUNNING

# Atomic compare-and-set terminal write. ``lost`` is one-way: a reader that finds
# a still-``running`` record whose liveness key has expired writes ``lost``, and
# the supervisor's own terminal write (``succeeded``/``failed``) must never
# overwrite it — nor may a late ``lost`` clobber a terminal the supervisor just
# wrote. Both writes go through this script, which transitions the record ONLY
# while its stored ``status`` is still ``running``, making the read-decide-write
# one atomic server-side step (a Python read-then-write across two awaits could
# interleave with the other writer).
#   KEYS[1] = run_key  # noqa: ERA001 (Lua param docs)
#   ARGV[1] = record TTL (seconds); ARGV[2..] = HSET field/value pairs
_TERMINAL_CAS_LUA = """
-- tool_runs:terminal-cas
if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return 0 end
redis.call('HSET', KEYS[1], unpack(ARGV, 2))
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""


class ToolRunStore:
    """Every tool-run key shape and the read/write operations behind one class.

    Operations take the redis client as an argument; each caller opens it from
    the tool-runs settings via ``client_ctx(RedisClient, settings.redis)``. Loud
    by contract — no swallowed errors, no silent fallback.
    """

    def __init__(self, key_prefix: str) -> None:
        """Bind the store to ``key_prefix``, which namespaces every key it builds."""
        self._p = key_prefix

    # -- key shapes ----------------------------------------------------------

    def run_key(self, run_id: str) -> str:
        """The record hash key for ``run_id``."""
        return f"{self._p}run:{run_id}"

    def liveness_key(self, run_id: str) -> str:
        """The liveness key for ``run_id``, present while the run's supervisor is alive."""
        return f"{self._p}live:{run_id}"

    def recent_key(self, tool_name: str, user_id: str | None = None) -> str:
        """The recent-runs index key for ``tool_name``.

        With ``user_id`` given, the PER-IDENTITY index ``recent:{user_id}:{tool_name}`` a
        restricted caller reads (its own complete window); without it, the shared
        ``recent:{tool_name}`` index an unrestricted caller reads.
        """
        if user_id is None:
            return f"{self._p}recent:{tool_name}"
        return f"{self._p}recent:{user_id}:{tool_name}"

    # -- writes --------------------------------------------------------------

    async def create_run(
        self,
        r: Any,
        run_id: str,
        tool_name: str,
        started_at: str,
        score: float,
        settings: ToolRunsSettings,
        user_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        crash_resume: bool = False,
    ) -> None:
        """Persist a new ``running`` record, prime its liveness key, and index it in the recent-runs ZSET.

        Trims the ZSET to the newest ``recent_runs_limit`` members. The record hash and the
        index both carry the record TTL so a tool that stops being run eventually drops its
        index.

        ``user_id`` is the OWNING identity of the run — always the caller's own id
        (each key is its own island). When present it is stamped onto
        the record AND the run id is also pushed onto the per-identity index
        ``recent:{user_id}:{tool_name}`` (its own bound/TTL, mirroring the shared
        index), so a restricted caller's list reads a complete window that other
        identities' volume can never truncate. A caller with no bound identity — an
        anonymous or gate-off REQUEST — leaves ``user_id`` absent and writes only the
        shared index; a fire always binds its execution key, gate off included, and is
        attributed to it.

        None of the writes branches on a prior result, so they are all issued in ONE
        pipeline (a single round trip) rather than sequentially.
        """
        run_key = self.run_key(run_id)
        recent_key = self.recent_key(tool_name)
        record: dict[str, str] = {"tool_name": tool_name, "status": _RUNNING, "started_at": started_at}
        if user_id is not None:
            record["user_id"] = user_id
        # Crash-resume seam: a run whose registration declared the generic crash-resume
        # flag persists its ``arguments`` (JSON) and a generic ``crash_resume`` marker, so
        # the liveness→lost reconciler can replay it FROM SCRATCH under the principal's
        # current live grants. An un-flagged run stores neither, writing only the base
        # record. The arguments are stored raw (not masked) because a from-scratch
        # replay must fire the exact recorded input.
        if crash_resume:
            record["crash_resume"] = "1"
            record["arguments"] = json.dumps(arguments or {})
        pipe = r.pipeline()
        pipe.hset(run_key, mapping=record)
        pipe.expire(run_key, settings.result_ttl_seconds)
        pipe.set(self.liveness_key(run_id), "1", ex=settings.liveness_ttl_seconds)
        pipe.zadd(recent_key, {run_id: score})
        # Trim to the newest N: rank 0..-(limit+1) is every member older than the
        # newest ``limit`` (lowest-scored first), removed in one call.
        pipe.zremrangebyrank(recent_key, 0, -(settings.recent_runs_limit + 1))
        pipe.expire(recent_key, settings.result_ttl_seconds)
        if user_id is not None:
            # The per-identity index mirrors the shared index's shape exactly (same
            # bound, same TTL) so a restricted caller's own window stays complete.
            user_key = self.recent_key(tool_name, user_id)
            pipe.zadd(user_key, {run_id: score})
            pipe.zremrangebyrank(user_key, 0, -(settings.recent_runs_limit + 1))
            pipe.expire(user_key, settings.result_ttl_seconds)
        await pipe.execute()

    async def refresh_liveness(self, r: Any, run_id: str, ttl: int) -> None:
        """Re-prime ``run_id``'s liveness key with a fresh ``ttl``."""
        await r.set(self.liveness_key(run_id), "1", ex=ttl)

    async def mark_terminal_if_running(self, r: Any, run_id: str, fields: dict[str, str], ttl: int) -> bool:
        """Compare-and-set terminal write, applied only while the stored ``status`` is still ``running``.

        Writes ``fields`` (the new ``status`` + ``finished_at`` and any ``result``/
        ``error``) and refreshes the record TTL, but ONLY while the stored ``status`` is
        still ``running`` — enforcing the one-way ``lost`` invariant atomically (see
        ``_TERMINAL_CAS_LUA``). Returns ``True`` when this call performed the transition,
        ``False`` when the record was no longer ``running`` (another writer reached a
        terminal state first).
        """
        flat: list[Any] = []
        for field, value in fields.items():
            flat.extend((field, value))
        written = await r.eval(_TERMINAL_CAS_LUA, 1, self.run_key(run_id), ttl, *flat)
        return bool(written)

    # -- reads ---------------------------------------------------------------

    async def get_run(self, r: Any, run_id: str) -> dict[str, str] | None:
        """The run record for ``run_id`` as a field map, or ``None`` when absent."""
        record = await r.hgetall(self.run_key(run_id))
        return record or None

    async def get_runs(self, r: Any, run_ids: list[str]) -> list[dict[str, str] | None]:
        """Batch ``HGETALL`` for many run ids in ONE pipeline, aligned to the input order.

        A vanished record maps to ``None`` (no per-id N+1).
        """
        if not run_ids:
            return []
        pipe = r.pipeline()
        for run_id in run_ids:
            pipe.hgetall(self.run_key(run_id))
        return [record or None for record in await pipe.execute()]

    async def liveness_present(self, r: Any, run_id: str) -> bool:
        """Whether ``run_id``'s liveness key is currently present."""
        return await r.get(self.liveness_key(run_id)) is not None

    async def liveness_present_many(self, r: Any, run_ids: list[str]) -> list[bool]:
        """Batch liveness-key ``GET`` for many run ids in ONE pipeline, aligned to the input order.

        Each entry is ``True`` when that run's liveness key is present.
        """
        if not run_ids:
            return []
        pipe = r.pipeline()
        for run_id in run_ids:
            pipe.get(self.liveness_key(run_id))
        return [value is not None for value in await pipe.execute()]

    async def recent_run_ids(self, r: Any, tool_name: str, limit: int, user_id: str | None = None) -> list[str]:
        """The most-recent-first run ids for ``tool_name``, up to ``limit``.

        Highest score (most recent start) first. With ``user_id`` given, reads the caller's
        per-identity index; without it, the shared index.
        """
        return await r.zrevrange(self.recent_key(tool_name, user_id), 0, limit - 1)

    async def prune_recent(self, r: Any, tool_name: str, run_id: str, user_id: str | None = None) -> None:
        """Drop ``run_id`` from the recent-runs index the matching list reads.

        Prunes the SAME index the list read: the per-identity index for a restricted caller
        (``user_id`` given), the shared index otherwise — so an expired entry is never pruned
        from the wrong index.
        """
        await r.zrem(self.recent_key(tool_name, user_id), run_id)
