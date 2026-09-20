"""Per-type close composes with lease accounting.

The lease mechanism is the single close policy — no per-type special-casing. These
tests verify each concrete impl's real ``_close`` composes with it: in-flight
leases finish before close (http/curl/redis/postgres/mcp close only fires
lease-idle), drain force-closes at the deadline, and the sync-redis close rides
``asyncio.to_thread``. The disconnection-eviction path is the one documented
exemption: a classified disconnection closes immediately regardless of held leases.

The network driver is never actually connected — clients are created offline and
closed without a live server.
"""

import asyncio

import pytest
from tai42_contract.errors import ClientDisconnectedError

from tai42_kit.clients import advance_client_epoch, drain_epoch
from tai42_kit.clients.base import PooledClient
from tai42_kit.clients.impl.http import HttpxClient


class _FakeConn:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False


def _make_client_cls():
    class _FakeClient(PooledClient):
        closed = 0

        async def _create(self, **kwargs):
            return _FakeConn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

    return _FakeClient


# ---------------------------------------------------------------------------
# httpx — close fires lease-idle so in-flight requests finish first
# ---------------------------------------------------------------------------
async def test_httpx_close_fires_only_when_lease_goes_idle():
    inst = HttpxClient()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def hold():
        async with inst.current(timeout=1.0) as client:
            holder["c"] = client
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    drain = asyncio.create_task(drain_epoch(retired, 5.0))
    await asyncio.sleep(0.02)  # drain is polling; the leased client stays open
    assert holder["c"].is_closed is False
    release.set()
    await task
    await drain
    assert holder["c"].is_closed is True  # closed once the lease went idle


async def test_httpx_force_closed_at_deadline_despite_held_lease():
    inst = HttpxClient()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def hold():
        async with inst.current(timeout=1.0) as client:
            holder["c"] = client
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    await drain_epoch(retired, 0.02)  # lease never releases -> force close at deadline
    assert holder["c"].is_closed is True
    release.set()
    await task
    assert holder["c"].is_closed is True  # the late release does not reopen/double-close


# ---------------------------------------------------------------------------
# curl_cffi — same lease-idle close on the real AsyncSession
# ---------------------------------------------------------------------------
async def test_curl_session_closes_on_lease_release_under_drain():
    pytest.importorskip("curl_cffi")
    from tai42_kit.clients.impl.curl import CurlClient

    inst = CurlClient()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def hold():
        async with inst.current(session_params={}) as session:
            holder["s"] = session
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    drain = asyncio.create_task(drain_epoch(retired, 5.0))
    await asyncio.sleep(0.02)
    assert holder["s"]._closed is False  # in-flight request not cut off
    release.set()
    await task
    await drain
    assert holder["s"]._closed is True


# ---------------------------------------------------------------------------
# redis — async aclose + the sync close offloaded to a worker thread
# ---------------------------------------------------------------------------
async def test_async_redis_closes_under_drain():
    pytest.importorskip("redis")
    from tai42_kit.clients.impl.redis import RedisClient

    inst = RedisClient()
    async with inst.current(url="redis://localhost:6379/1"):
        pass  # pooled under epoch 0 (no command issued -> no connection opened)
    retired = advance_client_epoch()
    await drain_epoch(retired, 0.0)  # RedisClient._close -> aclose, clean offline


async def test_sync_redis_close_offloads_to_thread(monkeypatch):
    pytest.importorskip("redis")
    from tai42_kit.clients.impl.redis import SyncRedisClient

    calls = []
    real_to_thread = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        calls.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)

    inst = SyncRedisClient()
    async with inst.current(url="redis://localhost:6379/1"):
        pass
    retired = advance_client_epoch()
    await drain_epoch(retired, 0.0)  # SyncRedisClient._close -> to_thread(client.close)
    assert calls, "sync close must be offloaded off the event loop"


# ---------------------------------------------------------------------------
# Disconnection-eviction exemption: immediate close regardless of held leases
# ---------------------------------------------------------------------------
async def test_disconnection_evicts_immediately_despite_concurrent_lease():
    cls = _make_client_cls()
    inst = cls()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with inst.current(url="a"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()  # a live lease is held on the pooled client
    # A concurrent lease on the same client hits a disconnection.
    with pytest.raises(ClientDisconnectedError):
        async with inst.current(url="a"):
            raise ConnectionError("dropped")
    assert cls.closed == 1  # closed immediately, exempt from close-on-last-release
    release.set()
    await task
    assert cls.closed == 1  # the holder's later release does not double-close


# ---------------------------------------------------------------------------
# postgres — the native pool close composes with leases (no live server needed:
# _create is stubbed to hand back a fake pool whose close() records the call)
# ---------------------------------------------------------------------------
class _FakePgPool:
    def __init__(self):
        self.closed = False

    async def close(self):  # the shape PostgresClient._close awaits
        self.closed = True


def _fake_postgres_cls():
    from tai42_kit.clients.impl.postgres import PostgresClient

    class _FakePostgresClient(PostgresClient):
        # Only _create is faked; the real PostgresClient._close (-> pool.close())
        # is exercised against the fake pool.
        async def _create(self, **kwargs):
            return _FakePgPool()

    return _FakePostgresClient


async def test_postgres_close_fires_on_last_lease_release():
    pytest.importorskip("psycopg_pool")
    inst = _fake_postgres_cls()()
    holder = {}
    async with inst.current(dsn="x") as pool:
        holder["p"] = pool
        retired = advance_client_epoch()  # retire the epoch mid-lease
        assert holder["p"].closed is False  # leased retired pool stays open
    assert holder["p"].closed is True  # real _close -> pool.close() on last release
    await drain_epoch(retired, 0.0)  # nothing left to drain


async def test_postgres_force_closed_at_deadline_despite_held_lease():
    pytest.importorskip("psycopg_pool")
    inst = _fake_postgres_cls()()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def hold():
        async with inst.current(dsn="x") as pool:
            holder["p"] = pool
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    await drain_epoch(retired, 0.02)  # lease never releases -> force close at deadline
    assert holder["p"].closed is True
    release.set()
    await task
    assert holder["p"].closed is True  # the late release does not double-close


# ---------------------------------------------------------------------------
# MCP — the shared fastmcp Client close (its __aexit__) waits for all leases
# (no live server: _create is stubbed to a fake whose __aexit__ records the call)
# ---------------------------------------------------------------------------
class _FakeMCPClient:
    def __init__(self):
        self.closed = False

    async def __aexit__(self, *exc):  # the shape FastMCPClient._close awaits
        self.closed = True


def _fake_mcp_cls():
    from tai42_kit.clients.impl.mcp import FastMCPClient

    class _FakeFastMCPClient(FastMCPClient):
        # Only _create is faked; the real FastMCPClient._close (-> __aexit__) is
        # exercised against the fake client.
        async def _create(self, **kwargs):
            return _FakeMCPClient()

    return _FakeFastMCPClient


async def test_mcp_close_waits_for_all_leases():
    pytest.importorskip("fastmcp")
    inst = _fake_mcp_cls()()
    entered_one = asyncio.Event()
    entered_two = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def hold(entered):
        # Both leases share the one pooled client for this config key.
        async with inst.current(config={"a": 1}) as client:
            holder["c"] = client
            entered.set()
            await release.wait()

    one = asyncio.create_task(hold(entered_one))
    await entered_one.wait()
    two = asyncio.create_task(hold(entered_two))
    await entered_two.wait()  # two concurrent leases on the shared client
    retired = advance_client_epoch()  # retire while both leases are held
    drain = asyncio.create_task(drain_epoch(retired, 5.0))
    await asyncio.sleep(0.02)
    assert holder["c"].closed is False  # shared client stays open while leased
    release.set()
    await asyncio.gather(one, two)
    await drain
    assert holder["c"].closed is True  # closed only once EVERY lease released


async def test_mcp_force_closed_at_deadline_despite_held_lease():
    pytest.importorskip("fastmcp")
    inst = _fake_mcp_cls()()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def hold():
        async with inst.current(config={"a": 1}) as client:
            holder["c"] = client
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    await drain_epoch(retired, 0.02)  # lease never releases -> force close at deadline
    assert holder["c"].closed is True
    release.set()
    await task
    assert holder["c"].closed is True  # the late release does not double-close
