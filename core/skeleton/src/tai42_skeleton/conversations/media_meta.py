"""The inbound-media metadata store on the conversations Redis (keyspace: ``media-meta``).

One hash per ingested media keyed by ``media_id`` carries the served blob's metadata plus the
two-phase pending/bound state; a sibling durable sorted-set index scores every ``media_id`` at
its retention horizon in EPOCH SECONDS. The hash holds NO Redis TTL — the durable index is the
enumerator the reaper walks, so a horizon never expires the hash out from under the reaper and
orphans its blob.

The two mutations (``put``, ``bind``) are SINGLE Lua scripts, so the hash write and the
expiry-index ZADD are ATOMIC: never a hash without its member (a permanent orphan), never a member
without its hash (a premature reap). ``bind`` does the ownership, expiry and bound-state checks and
the pending→bound flip + re-score in ONE script, returning a code the wrapper maps to the typed
errors.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

from pydantic import BaseModel
from tai42_contract.interactions.models import MediaKind
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.utils.redis_typing import awaited, eval_script

# The blob key prefix for every ingested media. The path is a pure function of the ``media_id``, so the
# ingest seam that writes the blob and the reaper that deletes it derive the SAME path from the id alone.
INBOUND_MEDIA_BLOB_PREFIX = "inbound-media/"


def inbound_media_blob_path(media_id: str) -> str:
    """The blob key for ``media_id`` — the one path both the ingest seam and the reaper address."""
    return f"{INBOUND_MEDIA_BLOB_PREFIX}{media_id}"


# Atomic write of the metadata hash AND its expiry-index member. The expiry score is validated
# numeric at the TOP, before any write, so a malformed score aborts the whole script leaving
# NEITHER the hash nor the member. ARGV = mime, size, sha256, filename, kind, storage_path,
# pending, owner_channel_id, owner_participant_identity, message_id, expiry_at, media_id.
_MEDIA_PUT_LUA = """
-- conversations:media-meta:put
local score = tonumber(ARGV[11])
if score == nil then return redis.error_reply('media-meta put: expiry score is not a number') end
redis.call('HSET', KEYS[1],
  'mime', ARGV[1], 'size', ARGV[2], 'sha256', ARGV[3], 'filename', ARGV[4], 'kind', ARGV[5],
  'storage_path', ARGV[6], 'pending', ARGV[7], 'owner_channel_id', ARGV[8],
  'owner_participant_identity', ARGV[9], 'message_id', ARGV[10])
redis.call('ZADD', KEYS[2], score, ARGV[12])
return 1
"""

# Atomic ownership + expiry + bound-state check and the pending->bound flip + re-score. Codes:
#  -1 unknown / expired / not-owned (one uniform code, no ownership oracle);
#  -2 already bound to a DIFFERENT message; 0 already bound to the SAME message (re-scored,
#  idempotent); 1 a pending record just bound. ARGV = message_id, channel_id,
#  participant_identity, now, expiry_at, media_id.
_MEDIA_BIND_LUA = """
-- conversations:media-meta:bind
local now = tonumber(ARGV[4])
if now == nil then return redis.error_reply('media-meta bind: now is not a number') end
local score = tonumber(ARGV[5])
if score == nil then return redis.error_reply('media-meta bind: expiry score is not a number') end
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local exp = redis.call('ZSCORE', KEYS[2], ARGV[6])
if (not exp) or tonumber(exp) < now then return -1 end
if redis.call('HGET', KEYS[1], 'owner_channel_id') ~= ARGV[2]
   or redis.call('HGET', KEYS[1], 'owner_participant_identity') ~= ARGV[3] then
  return -1
end
if redis.call('HGET', KEYS[1], 'pending') == '0' then
  if redis.call('HGET', KEYS[1], 'message_id') == ARGV[1] then
    redis.call('ZADD', KEYS[2], score, ARGV[6])
    return 0
  end
  return -2
end
redis.call('HSET', KEYS[1], 'pending', '0', 'message_id', ARGV[1])
redis.call('ZADD', KEYS[2], score, ARGV[6])
return 1
"""


class InboundMediaMeta(BaseModel):
    """One ingested media's stored metadata plus its two-phase state — a skeleton-internal shape.

    ``bind_media`` and the retention reaper read this ONE shape. ``pending`` is ``True`` until a
    message binds it; ``message_id`` is empty while pending. NOT a contract model.
    """

    media_id: str
    mime: str
    size: int
    sha256: str
    filename: str
    kind: MediaKind
    storage_path: str
    pending: bool
    owner_channel_id: str
    owner_participant_identity: str
    message_id: str | None = None


class InboundMediaMetaStore:
    """The metadata hash + durable expiry-index store on the conversations Redis."""

    __slots__ = ("settings",)

    def __init__(self, settings: ConversationsSettings) -> None:
        """Bind the conversations ``settings`` (the store shares the conversations Redis + prefix)."""
        self.settings = settings

    def meta_key(self, media_id: str) -> str:
        """The metadata hash key for ``media_id`` (a 43-char urlsafe id — no ``:`` — sits LAST)."""
        return f"{self.settings.prefix}:media-meta:{media_id}"

    @property
    def expiry_key(self) -> str:
        """The durable sorted-set expiry index — member = ``media_id``, score = horizon in epoch seconds."""
        return f"{self.settings.prefix}:media-meta:expiry"

    async def put(
        self,
        media_id: str,
        *,
        mime: str,
        size: int,
        sha256: str,
        filename: str,
        kind: MediaKind,
        storage_path: str,
        pending: bool,
        owner_channel_id: str,
        owner_participant_identity: str,
        message_id: str | None,
        expiry_at: float,
    ) -> None:
        """Write the metadata hash AND the expiry-index member in ONE atomic script."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await eval_script(
                r,
                _MEDIA_PUT_LUA,
                2,
                self.meta_key(media_id),
                self.expiry_key,
                mime,
                str(size),
                sha256,
                filename,
                kind.value,
                storage_path,
                "1" if pending else "0",
                owner_channel_id,
                owner_participant_identity,
                message_id or "",
                str(expiry_at),
                media_id,
            )

    async def bind(
        self,
        media_id: str,
        *,
        message_id: str,
        channel_id: str,
        participant_identity: str,
        now: float,
        expiry_at: float,
    ) -> int:
        """Run the atomic bind check + flip; return the code the wrapper maps to a typed result."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _MEDIA_BIND_LUA,
                    2,
                    self.meta_key(media_id),
                    self.expiry_key,
                    message_id,
                    channel_id,
                    participant_identity,
                    str(now),
                    str(expiry_at),
                    media_id,
                )
            )

    async def get(self, media_id: str) -> InboundMediaMeta | None:
        """The stored metadata for ``media_id``, or ``None`` when absent."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            hashed = await cast("Awaitable[dict[str, str]]", awaited(r.hgetall(self.meta_key(media_id))))
        if not hashed:
            return None
        return InboundMediaMeta(
            media_id=media_id,
            mime=hashed["mime"],
            size=int(hashed["size"]),
            sha256=hashed["sha256"],
            filename=hashed["filename"],
            kind=MediaKind(hashed["kind"]),
            storage_path=hashed["storage_path"],
            pending=hashed["pending"] == "1",
            owner_channel_id=hashed["owner_channel_id"],
            owner_participant_identity=hashed["owner_participant_identity"],
            message_id=hashed["message_id"] or None,
        )

    async def score(self, media_id: str) -> float | None:
        """The expiry-index score (the remaining-horizon source for the served route), or ``None``."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return await awaited(r.zscore(self.expiry_key, media_id))

    async def delete(self, media_id: str) -> None:
        """DEL the metadata hash AND ZREM the expiry member (the reaper deletes the blob first)."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.delete(self.meta_key(media_id)))
            await awaited(r.zrem(self.expiry_key, media_id))

    async def due_expiry_ids(self, now: float, *, limit: int = 500) -> list[str]:
        """Up to ``limit`` ``media_id``s whose expiry horizon is at or before ``now`` (the reaper's work list)."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return await awaited(r.zrangebyscore(self.expiry_key, 0, now, start=0, num=limit))
