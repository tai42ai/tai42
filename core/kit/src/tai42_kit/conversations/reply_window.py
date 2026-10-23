"""The reply-window store channel plugins share: one Redis key per pair holding the window's close time.

A channel whose medium allows free-form replies only for a while after the client's last message
keeps, per (our identity, client address) pair, one key whose value is the window's close time in
epoch milliseconds and which expires at that same instant (``SET ... PXAT``), so a closed window
needs no cleanup. :func:`advance_reply_window` moves the close time forward in one atomic script —
never back — so redelivered, out-of-order and concurrent inbounds on any replica cannot shorten
the window. :func:`read_reply_window` answers the pair's :class:`ReplyWindow`, whose close time is
whole seconds: the stored close time rounded down to its second.

The caller owns the key (its name space, its pair) and passes its own async Redis client; this
module knows no medium. The script needs Redis >= 6.2 (``PXAT``).
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from tai42_contract.channels import ReplyWindow

if TYPE_CHECKING:
    from redis.asyncio import Redis

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)
_MS_PER_SECOND = 1000

# KEYS[1] = the pair's key; ARGV = opened_at_ms, window_ms, now_ms. Writes the close time
# (opened_at + window) as both the value and the key's absolute expiry, unless the window has
# already closed or the stored close time is the same or later. A stored value that is not an
# integer is an error reply, never overwritten.
_ADVANCE_REPLY_WINDOW_LUA = (
    "local closes_at = tonumber(ARGV[1]) + tonumber(ARGV[2]) "
    "if closes_at <= tonumber(ARGV[3]) then return 0 end "
    "local stored = redis.call('GET', KEYS[1]) "
    "if stored then "
    "  local stored_ms = tonumber(stored) "
    "  if not stored_ms then return redis.error_reply('stored value is not an integer') end "
    "  if stored_ms >= closes_at then return 0 end "
    "end "
    "local value = string.format('%d', closes_at) "
    "redis.call('SET', KEYS[1], value, 'PXAT', value) "
    "return 1"
)


def _epoch_ms(value: datetime, name: str) -> int:
    """``value`` as whole epoch milliseconds; a naive datetime is refused."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got a naive {value!r}")
    return (value - _EPOCH) // _MILLISECOND


async def advance_reply_window(
    redis: Redis, key: str, *, opened_at: datetime, window_seconds: int, now: datetime
) -> None:
    """Move ``key``'s close time to ``opened_at + window_seconds`` when that is later than what is stored.

    A window that has already closed at ``now`` writes nothing; a stored close time that is the
    same or later is kept. The write sets the key to expire at the close time. ``opened_at`` and
    ``now`` must be timezone-aware and ``window_seconds`` positive (``ValueError`` otherwise); a
    stored value that is not an integer raises the script's ``ResponseError`` and is left as is.
    """
    if window_seconds <= 0:
        raise ValueError(f"window_seconds must be positive, got {window_seconds}")
    opened_at_ms = _epoch_ms(opened_at, "opened_at")
    now_ms = _epoch_ms(now, "now")
    await cast(
        "Awaitable[int]",
        redis.eval(_ADVANCE_REPLY_WINDOW_LUA, 1, key, str(opened_at_ms), str(window_seconds * 1000), str(now_ms)),
    )


async def read_reply_window(redis: Redis, key: str, *, now: datetime) -> ReplyWindow:
    """The pair's window at ``now``: ``open`` until the stored close time rounded down to its second, else ``closed``.

    The reported close time is that whole second, so the window is never reported longer than it
    is. A missing key (never opened, or expired) and a whole-second close time at or before ``now``
    read as ``closed``. A stored value that is not an integer raises ``ValueError``; it is never read as
    closed. ``now`` must be timezone-aware.
    """
    now_ms = _epoch_ms(now, "now")
    stored = await cast("Awaitable[str | bytes | None]", redis.get(key))
    if stored is None:
        return ReplyWindow(state="closed", closes_at=None)
    try:
        stored_ms = int(stored)
    except ValueError:
        raise ValueError("the stored reply-window close time is not an integer") from None
    closes_at_ms = stored_ms - stored_ms % _MS_PER_SECOND
    if closes_at_ms <= now_ms:
        return ReplyWindow(state="closed", closes_at=None)
    return ReplyWindow(state="open", closes_at=_EPOCH + timedelta(milliseconds=closes_at_ms))
