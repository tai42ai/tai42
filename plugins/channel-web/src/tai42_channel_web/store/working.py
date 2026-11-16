"""The live working-on-it frame.

One Redis STREAM per conversation pair holding only the latest ``chat.working`` frame.
Each append trims the stream to that one entry and sets the key to expire one frame
lifetime later, so a process that dies mid-assert leaves no frame alive longer than
its lifetime. Nothing here enters the transcript: a backlog replay never carries a
working frame, and the SSE tail reads this stream beside the transcript.
"""

from __future__ import annotations

import math

from tai42_channel_web.store.connection import _redis
from tai42_channel_web.store.transcript import frame

WORKING_EVENT = "chat.working"


def working_key(identity: str, address: str) -> str:
    """The Redis key of the pair's working stream."""
    return f"channel:web:working:{identity}:{address}"


async def append_working(identity: str, address: str, *, active: bool, ttl_seconds: float) -> None:
    """Write one working frame for the pair: the stream keeps it alone and dies ``ttl_seconds`` later.

    One pipeline, so the trim and the expiry cannot be left behind by a lost second command.
    """
    key = working_key(identity, address)
    async with _redis() as redis:
        pipe = redis.pipeline()
        pipe.xadd(key, {"active": "1" if active else "0", "ttl": str(ttl_seconds)}, maxlen=1, approximate=False)
        pipe.expire(key, math.ceil(ttl_seconds))
        await pipe.execute()


def working_frame(entry_id: str, fields: dict[str, str]) -> str | None:
    """The stored entry as its ``chat.working`` SSE frame, or ``None`` when its fields are malformed.

    The frame carries the entry ``id``, ``active``, and ``ttl`` — the seconds the page shows
    the frame for when no later frame arrives.
    """
    active = fields.get("active")
    if active not in ("1", "0"):
        return None
    try:
        ttl = float(fields["ttl"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(ttl) or ttl <= 0:
        return None
    return frame(WORKING_EVENT, {"id": entry_id, "active": active == "1", "ttl": ttl})
