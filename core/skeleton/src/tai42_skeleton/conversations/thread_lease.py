"""The cross-worker per-thread turn mutex.

A turn forks a thread's agent checkpoint: it reads the parent memory, runs, and writes the
child back. Two workers running a turn on the SAME thread at once each fork the same parent
and the last write wins, dropping the other turn's memory. The per-worker FIFO in
:mod:`tai42_skeleton.conversations.caps` serializes only within one worker; this is the
mutex that serializes ACROSS workers.

It is a token-fenced Redis lease in the conversations Redis, held for the whole
``run_reserved`` span and heartbeat-refreshed while the turn runs; a crashed holder is
recovered by TTL expiry. The idiom mirrors the intake lease
(:data:`~tai42_skeleton.conversations.record_scripts._CLAIM_INTAKE_LUA` and its refresher).

A release is announced on the release channel
(:attr:`~tai42_skeleton.conversations.settings.ConversationsSettings.thread_lease_released_channel`).
A turn that finds the thread busy waits on its event loop's release listener and retries the
moment the holder releases, or when the holder's lease would lapse (a crashed holder).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from asyncio import AbstractEventLoop
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any
from uuid import uuid4
from weakref import WeakKeyDictionary

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import UnavailableError
from tai42_skeleton.utils.redis_typing import eval_script

logger = logging.getLogger(__name__)


class ThreadLeaseLostError(UnavailableError):
    """The thread turn lease was lost mid-turn (its TTL lapsed or another worker adopted it).

    A loud, retriable 503 — the turn cannot trust its parent checkpoint any longer.
    """


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

# Take the lease when it is free: {1, 0} taken; {0, pttl} held by another token for pttl more
# milliseconds (-1 when the key carries no expiry). KEYS[1]=lease key; ARGV = token, lease_ms.
_ACQUIRE_LUA = """
-- conversations:thread_lease:acquire
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return {1, 0}
end
return {0, redis.call('PTTL', KEYS[1])}
"""

# Release the lease only while this worker's token still holds it, so a lapsed holder never
# deletes the adopter's lease, and announce the release to the waiting turns.
# KEYS[1]=lease key; ARGV = token, release channel, thread id.
_RELEASE_LUA = """
-- conversations:thread_lease:release
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('PUBLISH', ARGV[2], ARGV[3])
  return 1
end
return 0
"""

# The listener's re-subscribe backoff after a lost subscription: seconds, doubling to the ceiling.
_RESUBSCRIBE_BACKOFF_INITIAL = 1.0
_RESUBSCRIBE_BACKOFF_MAX = 30.0


async def _wait_for_release(woken: asyncio.Event, seconds: float) -> None:
    """Wait until ``woken`` is set (a release, or a listener re-subscribe) or ``seconds`` pass.

    The bound is the holder's remaining lease: at its end a crashed holder's lease has lapsed,
    and a live holder's refresh has extended it, so the waiter retries either way.
    """
    try:
        async with asyncio.timeout(seconds):
            await woken.wait()
    except TimeoutError:
        # The holder's lease ran its course without a release: retry now.
        return


class _LeaseReleaseListener:
    """One event loop's subscriber to a release channel, subscribed while a turn waits on it.

    The first waiter starts the subscriber task and enters once the subscription is confirmed, so
    no release published after its own retry is missed; the last waiter to leave stops the task
    and closes the subscription. Every release wakes the waiters registered for its thread id.
    """

    def __init__(self, channel: str, settings: Callable[[], ConversationsSettings]) -> None:
        self._channel = channel
        self._settings = settings
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._task: asyncio.Task[None] | None = None
        self._subscribed: asyncio.Future[None] | None = None

    @asynccontextmanager
    async def waiting(self, thread_id: str) -> AsyncIterator[asyncio.Event]:
        """Register a wake-up event for ``thread_id`` and yield it once the listener is subscribed.

        A first subscription that fails raises here, to every waiter entering on it.
        """
        woken = asyncio.Event()
        self._waiters.setdefault(thread_id, set()).add(woken)
        try:
            subscribed = self._subscribed
            if self._task is None or self._task.done() or subscribed is None:
                subscribed = self._subscribed = asyncio.get_running_loop().create_future()
                self._task = asyncio.create_task(
                    self._run(subscribed), name=f"conversations-thread-lease-listener:{self._channel}"
                )
            await asyncio.shield(subscribed)
            yield woken
        finally:
            await self._leave(thread_id, woken)

    async def _leave(self, thread_id: str, woken: asyncio.Event) -> None:
        events = self._waiters.get(thread_id)
        if events is not None:
            events.discard(woken)
            if not events:
                del self._waiters[thread_id]
        if self._waiters or self._task is None:
            return
        task, self._task, self._subscribed = self._task, None, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Only the listener's own cancellation ends here; a cancel delivered to this waiter
            # while it waits for the listener to close is re-raised.
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise

    def _wake(self, thread_id: str) -> None:
        for woken in self._waiters.get(thread_id, ()):
            woken.set()

    def _wake_all(self) -> None:
        for events in self._waiters.values():
            for woken in events:
                woken.set()

    async def _run(self, subscribed: asyncio.Future[None]) -> None:
        backoff = _RESUBSCRIBE_BACKOFF_INITIAL

        def _on_subscribed() -> None:
            nonlocal backoff
            backoff = _RESUBSCRIBE_BACKOFF_INITIAL
            if not subscribed.done():
                subscribed.set_result(None)
                return
            # A re-subscribe: a release published while the subscription was down was not
            # heard, so every waiter retries now.
            self._wake_all()

        while True:
            try:
                await self._listen(_on_subscribed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not subscribed.done():
                    subscribed.set_exception(exc)
                    return
                logger.error(
                    "conversations: thread lease release listener lost its subscription; re-subscribing",
                    exc_info=True,
                )
                self._wake_all()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RESUBSCRIBE_BACKOFF_MAX)

    async def _listen(self, on_subscribed: Callable[[], None]) -> None:
        redis = self._settings().redis
        # A dedicated connection with no read timeout: the listener waits for a release for as
        # long as a turn waits, and a pooled connection would be pinned for that whole wait.
        listener_redis = redis.model_copy(update={"socket_timeout": None})
        async with client_ctx(RedisClient, listener_redis, fresh=True) as r:
            pubsub = r.pubsub()
            try:
                await pubsub.subscribe(self._channel)
                # The confirmation read is bounded by the configured read timeout, so a Redis that
                # accepts the connection but never answers fails the subscribe loudly.
                confirmation = await pubsub.get_message(timeout=redis.socket_timeout)
                if confirmation is None or confirmation.get("type") != "subscribe":
                    raise ConnectionError(
                        f"conversations: no subscription confirmation for thread lease release channel "
                        f"{self._channel!r}: {confirmation!r}"
                    )
                on_subscribed()
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
                    if message is not None:
                        self._wake(_text(message["data"]))
            finally:
                await pubsub.aclose()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


# Per event loop and channel: a listener's subscriber task and events belong to one loop.
_listeners: WeakKeyDictionary[AbstractEventLoop, dict[str, _LeaseReleaseListener]] = WeakKeyDictionary()
_listeners_lock = threading.Lock()


def _release_listener(settings: Callable[[], ConversationsSettings]) -> _LeaseReleaseListener:
    """The running loop's listener on the current release channel, made on first use."""
    loop = asyncio.get_running_loop()
    channel = settings().thread_lease_released_channel
    with _listeners_lock:
        per_loop = _listeners.setdefault(loop, {})
        listener = per_loop.get(channel)
        if listener is None:
            listener = per_loop[channel] = _LeaseReleaseListener(channel, settings)
        return listener


class _LeaseSignal:
    """Carries the heartbeat's lost verdict back to the owner so a self-cancel is told apart from an external one."""

    __slots__ = ("lost",)

    def __init__(self) -> None:
        self.lost = False


class ThreadTurnLease:
    """The cross-worker per-thread turn mutex over the conversations Redis.

    Holds no live state of its own and keeps no settings instance: every read goes through
    ``settings`` — a zero-argument source of the current :class:`ConversationsSettings` — so a
    settings reload is adopted at the next read, mid-span included.
    """

    def __init__(self, settings: Callable[[], ConversationsSettings]) -> None:
        """Bind to the settings source ``settings``; every timing is read through it at its use."""
        self._settings = settings

    @asynccontextmanager
    async def held(self, thread_id: str) -> AsyncIterator[None]:
        """Hold ``thread_id``'s cross-worker lease for the body's duration, heartbeat-refreshed while it runs.

        A lease lost mid-body cancels the body and surfaces as :class:`ThreadLeaseLostError`.
        """
        if self._settings().in_memory:
            # No conversations Redis is no cross-process store, and a worker without one can
            # never run a turn — the mutex is an explicit local no-op, not a hidden skip.
            yield
            return
        key = self._settings().thread_lease_key(thread_id)
        token = uuid4().hex
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("ThreadTurnLease.held must run inside a task to be heartbeat-refreshed")
        signal = _LeaseSignal()
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self._acquire(key, token, thread_id)
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
            await self._release(key, token, thread_id)
            if pending_owner_cancel:
                raise asyncio.CancelledError

    async def _acquire(self, key: str, token: str, thread_id: str) -> None:
        """Take the lease; while another worker holds it, wait for its release and retry.

        A free lease is taken in one round trip. A held one is waited for on the loop's release
        listener: each retry clears the wake-up first, so a release landing between the retry and
        the wait still wakes it, and each wait lasts at most the holder's remaining lease. The
        wait is unbounded — the same shape as a local waiter behind a HITL-paused turn; the
        upstream FIFO depth bounds how many ever queue here.
        """
        held_ms = await self._try_acquire(key, token)
        if held_ms is None:
            return
        async with _release_listener(self._settings).waiting(thread_id) as woken:
            while True:
                woken.clear()
                held_ms = await self._try_acquire(key, token)
                if held_ms is None:
                    return
                await _wait_for_release(woken, held_ms / 1000)

    async def _try_acquire(self, key: str, token: str) -> int | None:
        """Take the lease if free (``None``); else the milliseconds to wait for it at most.

        That is the holder's remaining lease, or one full lease when the key carries no expiry.
        """
        lease_ms = self._settings().thread_lease_seconds * 1000
        async with client_ctx(RedisClient, self._settings().redis) as r:
            taken, pttl = await eval_script(r, _ACQUIRE_LUA, 1, key, token, lease_ms)
        if int(taken) == 1:
            return None
        return lease_ms if int(pttl) == -1 else max(int(pttl), 0)

    async def _heartbeat(
        self, key: str, token: str, owner: asyncio.Task[object], signal: _LeaseSignal, last_success: float
    ) -> None:
        """Re-expire the lease every ``thread_lease_refresh_seconds`` until it is lost.

        A transient Redis error is logged and retried, but only while under ``last_success +
        thread_lease_seconds``: once that monotonic deadline passes the lease may already have
        lapsed server-side and been adopted, so the hold can no longer be proven and the owner
        is cancelled exactly as a returned-0 does. A lost lease cancels the owner, whose
        context turns the self-cancel into :class:`ThreadLeaseLostError`.
        """
        while True:
            await asyncio.sleep(self._settings().thread_lease_refresh_seconds)
            try:
                held = await self._refresh(key, token)
            except Exception:
                lease_seconds = self._settings().thread_lease_seconds
                if monotonic() - last_success >= lease_seconds:
                    logger.warning(
                        "conversations: thread lease %s unreachable for its full TTL (%ss since the last proven "
                        "refresh); it may be adopted — cancelling the turn, its outcome is another worker's to write",
                        key,
                        lease_seconds,
                    )
                    signal.lost = True
                    owner.cancel()
                    return
                logger.error(
                    "conversations: refreshing thread lease %s failed; retrying in %ss",
                    key,
                    self._settings().thread_lease_refresh_seconds,
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
        lease_ms = self._settings().thread_lease_seconds * 1000
        async with client_ctx(RedisClient, self._settings().redis) as r:
            return int(await eval_script(r, _REFRESH_LUA, 1, key, token, lease_ms))

    async def _release(self, key: str, token: str, thread_id: str) -> None:
        try:
            settings = self._settings()
            async with client_ctx(RedisClient, settings.redis) as r:
                await eval_script(r, _RELEASE_LUA, 1, key, token, settings.thread_lease_released_channel, thread_id)
        except Exception:
            logger.error(
                "conversations: releasing thread lease %s failed; its TTL is the hard bound",
                key,
                exc_info=True,
            )


__all__ = ["ThreadLeaseLostError", "ThreadTurnLease"]
