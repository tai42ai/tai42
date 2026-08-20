"""The cross-worker per-thread turn mutex.

A turn forks a thread's agent checkpoint: it reads the parent memory, runs, and writes the
child back. Two workers running a turn on the SAME thread at once each fork the same parent
and the last write wins, dropping the other turn's memory. The per-worker FIFO in
:mod:`tai42_skeleton.conversations.caps` serializes only within one worker; this is the
mutex that serializes ACROSS workers.

It is a token-fenced Redis lease in the conversations Redis, held for the whole
``run_reserved`` span and heartbeat-refreshed while the turn runs; a crashed holder is
recovered by TTL expiry. The idiom mirrors the intake lease
(:data:`~tai42_skeleton.conversations.records._CLAIM_INTAKE_LUA` and its refresher).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import UnavailableError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)


class ThreadLeaseLostError(UnavailableError):
    """The thread turn lease was lost mid-turn (its TTL lapsed or another worker adopted it):
    a loud, retriable 503 — the turn cannot trust its parent checkpoint any longer."""


# Refresh the lease under this worker's token: 1 still held, 0 lost (expired or taken).
# KEYS[1]=lease key; ARGV = token, lease_ms.
_REFRESH_LUA = """
-- conversations:thread_lease:refresh
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

# Release the lease only while this worker's token still holds it, so a lapsed holder never
# deletes the adopter's lease. KEYS[1]=lease key; ARGV = token.
_RELEASE_LUA = """
-- conversations:thread_lease:release
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _LeaseSignal:
    """Carries the heartbeat's lost verdict back to the owner so a self-cancel is told apart
    from an external one."""

    __slots__ = ("lost",)

    def __init__(self) -> None:
        self.lost = False


class ThreadTurnLease:
    """The cross-worker per-thread turn mutex over the conversations Redis. Holds no live
    state of its own — every :meth:`held` span reads the current timings, so a settings
    reload is adopted in place by :meth:`reconfigure`."""

    def __init__(self, settings: ConversationsSettings) -> None:
        self.settings = settings

    def reconfigure(self, settings: ConversationsSettings) -> None:
        """Adopt a reloaded settings snapshot in place; the next span uses the new timings."""
        self.settings = settings

    @asynccontextmanager
    async def held(self, thread_id: str) -> AsyncIterator[None]:
        """Hold ``thread_id``'s cross-worker lease for the body's duration, heartbeat-refreshed
        while it runs. A lease lost mid-body cancels the body and surfaces as
        :class:`ThreadLeaseLostError`."""
        if self.settings.in_memory:
            # No conversations Redis is no cross-process store, and a worker without one can
            # never run a turn — the mutex is an explicit local no-op, not a hidden skip.
            yield
            return
        key = self.settings.thread_lease_key(thread_id)
        token = uuid4().hex
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("ThreadTurnLease.held must run inside a task to be heartbeat-refreshed")
        signal = _LeaseSignal()
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self._acquire(key, token)
            heartbeat = asyncio.create_task(self._heartbeat(key, token, owner, signal, monotonic()))
            try:
                yield
            except asyncio.CancelledError:
                if signal.lost:
                    # This worker's own cancel from a lost lease — translate it; the task is
                    # un-cancelled so the raised error propagates as a normal 503, while a
                    # cancel from anywhere else passes through untouched.
                    if hasattr(owner, "uncancel"):
                        owner.uncancel()
                    raise ThreadLeaseLostError(
                        f"conversation thread {thread_id!r} turn lease was lost mid-turn (expired or taken by "
                        f"another worker); retry once the holder drains"
                    ) from None
                raise
        finally:
            pending_owner_cancel = False
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    # Only the heartbeat's own cancellation is swallowed here; a cancel
                    # delivered to the owner in this window (a real shutdown) leaves
                    # ``cancelling() > 0`` and is re-raised after the lease is released, never
                    # absorbed by this cleanup.
                    if owner.cancelling() > 0:
                        pending_owner_cancel = True
            # Token-guarded and unconditional: a SET that won server-side but was cancelled
            # before the body was entered is released here, and a lease we never held is a
            # no-op (the token never matches).
            await self._release(key, token)
            if pending_owner_cancel:
                raise asyncio.CancelledError

    async def _acquire(self, key: str, token: str) -> None:
        """SET NX PX, polling every ``thread_lease_poll_seconds`` until the key is free. The
        wait is unbounded — the same shape as a local waiter behind a HITL-paused turn; the
        upstream FIFO depth bounds how many ever queue here."""
        lease_ms = self.settings.thread_lease_seconds * 1000
        while True:
            async with client_ctx(RedisClient, self.settings.redis) as r:
                won = await awaited(r.set(key, token, nx=True, px=lease_ms))
            if won is not None:
                return
            await asyncio.sleep(self.settings.thread_lease_poll_seconds)

    async def _heartbeat(
        self, key: str, token: str, owner: asyncio.Task[object], signal: _LeaseSignal, last_success: float
    ) -> None:
        """Re-expire the lease every ``thread_lease_refresh_seconds`` until it is lost. A
        transient Redis error is logged and retried, but only while under ``last_success +
        thread_lease_seconds``: once that monotonic deadline passes the lease may already have
        lapsed server-side and been adopted, so the hold can no longer be proven and the owner
        is cancelled exactly as a returned-0 does. A lost lease cancels the owner, whose
        context turns the self-cancel into :class:`ThreadLeaseLostError`."""
        while True:
            await asyncio.sleep(self.settings.thread_lease_refresh_seconds)
            try:
                held = await self._refresh(key, token)
            except Exception:
                if monotonic() - last_success >= self.settings.thread_lease_seconds:
                    logger.warning(
                        "conversations: thread lease %s unreachable for its full TTL (%ss since the last proven "
                        "refresh); it may be adopted — cancelling the turn, its outcome is another worker's to write",
                        key,
                        self.settings.thread_lease_seconds,
                    )
                    signal.lost = True
                    owner.cancel()
                    return
                logger.error(
                    "conversations: refreshing thread lease %s failed; retrying in %ss",
                    key,
                    self.settings.thread_lease_refresh_seconds,
                    exc_info=True,
                )
                continue
            if held != 1:
                logger.warning(
                    "conversations: thread lease %s no longer holds this worker's token (refresh returned %d); "
                    "cancelling the turn — its outcome is another worker's to write",
                    key,
                    held,
                )
                signal.lost = True
                owner.cancel()
                return
            last_success = monotonic()

    async def _refresh(self, key: str, token: str) -> int:
        lease_ms = self.settings.thread_lease_seconds * 1000
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(await eval_script(r, _REFRESH_LUA, 1, key, token, lease_ms))

    async def _release(self, key: str, token: str) -> None:
        try:
            async with client_ctx(RedisClient, self.settings.redis) as r:
                await eval_script(r, _RELEASE_LUA, 1, key, token)
        except Exception:
            logger.error(
                "conversations: releasing thread lease %s failed; its TTL is the hard bound",
                key,
                exc_info=True,
            )


__all__ = ["ThreadLeaseLostError", "ThreadTurnLease"]
