"""PooledClient lifecycle edge paths: disconnection eviction, close misses, the
base NotImplementedError stubs, the facade settings-xor-kwargs guard, and the
shutdown_all_clients early-exit + ExceptionGroup error path.

All in-process — no external driver. A fresh subclass per test keeps the
module-global, class-keyed pool from leaking state across tests.
"""

import asyncio

import pytest
from tai42_contract.errors import ClientDisconnectedError

from tai42_kit.clients import ClientSettings, PooledClient, client_ctx, shutdown_all_clients


class _Conn:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False


def _make_client_cls(*, close_raises: bool = False):
    class _FakeClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            return _Conn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True
            if close_raises:
                raise RuntimeError("close boom")

        def _disconnection_exceptions(self):
            return (ConnectionError,)

    return _FakeClient


async def test_base_create_and_close_are_not_implemented():
    base = PooledClient()
    with pytest.raises(NotImplementedError):
        await base._create()
    with pytest.raises(NotImplementedError):
        await base._close(object())


def test_default_disconnection_exceptions():
    # The base default set is ConnectionError only: cancellation is excluded so a
    # cancelled `current()` body re-raises instead of being converted into a
    # ClientDisconnectedError. Concrete clients override with their driver's set.
    assert PooledClient()._disconnection_exceptions() == (ConnectionError,)
    assert asyncio.CancelledError not in PooledClient()._disconnection_exceptions()


def test_default_predicate_is_isinstance_against_set():
    # The base predicate matches exactly the _disconnection_exceptions() set;
    # anything else (RuntimeError, cancellation) is not a disconnection.
    base = PooledClient()
    assert base._is_disconnection_error(ConnectionError("dropped")) is True
    assert base._is_disconnection_error(RuntimeError("Event loop is closed")) is False
    assert base._is_disconnection_error(asyncio.CancelledError()) is False


def test_key_is_order_independent_json():
    # The pool key is canonical JSON, so kwarg order does not split the pool.
    assert PooledClient._key(a=1, b=2) == PooledClient._key(b=2, a=1)


async def test_disconnection_evicts_closes_and_raises_wrapped():
    cls = _make_client_cls()

    async def use_and_drop():
        async with cls().current(url="a") as conn:
            assert conn.closed is False
            raise ConnectionError("dropped")

    with pytest.raises(ClientDisconnectedError) as ei:
        await use_and_drop()
    # The dead client was closed and removed from the cache.
    assert cls.closed == 1
    assert "disconnected" in str(ei.value)
    # The original error is chained, never swallowed.
    assert isinstance(ei.value.__cause__, ConnectionError)
    # Next use rebuilds a fresh client (the cache entry was evicted).
    async with cls().current(url="a"):
        pass
    assert cls.created == 2


async def test_disconnection_close_failure_still_raises_disconnected():
    # If closing the dead client itself fails, the close error is swallowed at
    # debug but the ClientDisconnectedError still surfaces (never masked).
    cls = _make_client_cls(close_raises=True)
    with pytest.raises(ClientDisconnectedError):
        async with cls().current(url="a"):
            raise ConnectionError("dropped")
    assert cls.closed == 1


async def test_non_disconnection_error_propagates_without_eviction():
    cls = _make_client_cls()
    with pytest.raises(ValueError, match="other"):
        async with cls().current(url="a"):
            raise ValueError("other")
    # A non-disconnection error leaves the pooled client in place (not closed).
    assert cls.closed == 0
    async with cls().current(url="a"):
        pass
    assert cls.created == 1  # reused, not rebuilt


async def test_cancelled_error_reraises_without_eviction():
    cls = _make_client_cls()
    with pytest.raises(asyncio.CancelledError):
        async with cls().current(url="a"):
            raise asyncio.CancelledError
    # Cancellation is never converted into a disconnection: no close, no evict.
    assert cls.closed == 0
    async with cls().current(url="a"):
        pass
    assert cls.created == 1  # reused, not rebuilt


async def test_overridden_predicate_drives_eviction():
    # current() consults _is_disconnection_error, so an impl can match a broad
    # exception type by message: only matching RuntimeErrors evict the client,
    # any other RuntimeError from caller code propagates untouched.
    class _MsgClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            return _Conn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

        def _is_disconnection_error(self, exc: BaseException) -> bool:
            return isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)

    with pytest.raises(RuntimeError, match="caller bug"):
        async with _MsgClient().current(url="a"):
            raise RuntimeError("caller bug")
    assert _MsgClient.closed == 0  # non-matching RuntimeError: no teardown

    with pytest.raises(ClientDisconnectedError):
        async with _MsgClient().current(url="a"):
            raise RuntimeError("Event loop is closed")
    assert _MsgClient.closed == 1  # matching message: evicted and closed


async def test_reuse_hits_cache_without_recreating():
    cls = _make_client_cls()
    async with cls().current(url="a") as c1:
        pass
    # Second top-level use of the same key finds the pooled client (no rebuild).
    async with cls().current(url="a") as c2:
        assert c2 is c1
    assert cls.created == 1


async def test_concurrent_first_use_creates_one_client():
    # Two concurrent first-use callers on the same key: the double-checked lock
    # must build exactly one client (the second wakes after the lock and finds
    # it already cached), so none is orphaned.
    class _SlowClient(PooledClient):
        created = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            await asyncio.sleep(0.01)
            return _Conn(**kwargs)

        async def _close(self, client):
            pass

    async def _use():
        async with _SlowClient().current(url="a") as c:
            return c

    c1, c2 = await asyncio.gather(_use(), _use())
    assert c1 is c2
    assert _SlowClient.created == 1


async def test_close_missing_key_is_noop():
    cls = _make_client_cls()
    # Closing a key that was never opened does nothing and does not raise.
    await cls().close(url="never")
    assert cls.closed == 0


async def test_facade_rejects_settings_and_kwargs_together():
    cls = _make_client_cls()

    class _S(ClientSettings):
        def client_kwargs(self):
            return {"url": "x"}

    with pytest.raises(ValueError, match="either `settings` or"):
        client_ctx(cls, _S(), url="y")


async def test_shutdown_all_clients_empty_is_noop():
    # No live pools on this loop -> early return, no error.
    await shutdown_all_clients()


async def test_shutdown_during_in_flight_create_does_not_orphan_client():
    # Interleave: a `current()` enter is mid-`_create` (client built but not yet
    # pooled) when `shutdown_all_clients()` runs on the same loop. Shutdown
    # detaches the loop's (empty) pool and closes nothing. The created client
    # must NOT be written into the detached, now-unreachable dict (that would
    # orphan it, never closed); it must land in the live pool, closable by a
    # later shutdown.
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            started.set()
            await release.wait()  # suspend mid-create so shutdown can interleave
            return _Conn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

    used = {}

    async def _use():
        async with _SlowClient().current(url="a") as conn:
            used["conn"] = conn
            return conn

    task = asyncio.create_task(_use())
    await started.wait()  # create is in flight, before the client is pooled

    # Shutdown interleaves here: the inner pool dict exists but is empty.
    await shutdown_all_clients()
    assert _SlowClient.closed == 0  # nothing pooled yet, so nothing to close

    release.set()
    conn = await task  # create finishes; the client must be pooled, not orphaned
    assert conn is used["conn"]
    assert conn.closed is False

    # The created client is reachable and closable: a later shutdown closes it.
    # Without the re-fetch fix it would sit in the detached dict and leak here.
    await shutdown_all_clients()
    assert _SlowClient.closed == 1
    assert conn.closed is True


async def test_two_waiters_with_shutdown_mid_create_no_duplicate_or_orphan():
    # Two waiters race on the same key. Waiter A holds the per-(class, key) create
    # lock and is suspended mid-`_create` when `shutdown_all_clients()` runs on
    # the same loop, detaching and clearing this loop's pool dict. A's create then
    # finishes and its client lands in the re-registered pool. Waiter B captured
    # that now-stale, cleared dict BEFORE parking on the (surviving) lock; after
    # acquiring the lock it must re-check against the LIVE pool — finding A's
    # client already there — so it creates no duplicate and orphans nothing.
    #
    # Without the re-fetch at the top of the lock block, B re-checks the stale
    # cleared dict, finds the key absent, builds a second client, and overwrites
    # A's client in the re-registered dict (A orphaned, never closed).
    started = asyncio.Event()
    release = asyncio.Event()
    first = {"taken": False}

    class _SlowClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            if not first["taken"]:
                first["taken"] = True
                started.set()
                await release.wait()  # suspend the FIRST creator mid-create
            return _Conn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

    async def _use():
        async with _SlowClient().current(url="a") as c:
            return c

    a = asyncio.create_task(_use())
    await started.wait()  # A holds the create lock, suspended inside _create

    b = asyncio.create_task(_use())  # B captures the pool dict, parks on the lock
    await asyncio.sleep(0)  # let B run up to the lock acquire and park

    # Shutdown interleaves: detaches + clears this loop's pool (empty so far).
    await shutdown_all_clients()
    assert _SlowClient.closed == 0  # nothing pooled yet, so nothing to close

    release.set()
    ca = await a
    cb = await b

    # Exactly one client created; both waiters share it; none orphaned.
    assert _SlowClient.created == 1
    assert ca is cb

    # The single client is reachable and closable by a later shutdown.
    await shutdown_all_clients()
    assert _SlowClient.closed == 1
    assert ca.closed is True


async def test_shutdown_all_clients_collects_errors_into_group():
    cls = _make_client_cls(close_raises=True)
    async with cls().current(url="a"):
        pass
    async with cls().current(url="b"):
        pass
    with pytest.raises(ExceptionGroup) as ei:
        await shutdown_all_clients()
    # Every close was attempted; both failures collected, none dropped.
    assert cls.closed == 2
    assert len(ei.value.exceptions) == 2
    assert all(isinstance(e, RuntimeError) for e in ei.value.exceptions)
