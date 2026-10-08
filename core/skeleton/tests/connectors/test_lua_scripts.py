"""End-to-end execution of the load-bearing Redis Lua scripts.

The store's cache-coherence fences (version-fenced set-if-newer, delete
tombstone) and the connection lock's compare-and-delete release are Lua run
server-side by Redis. The rest of the suite drives them through Python fakes;
this module runs the REAL script text against a Lua-capable Redis stand-in
(``fakeredis[lua]``) so a bug in the actual Lua is caught, not modelled away.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis import aioredis
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.connectors.runtime.locks as locks
import tai42_skeleton.connectors.store.redis_pg as redis_pg
from tai42_skeleton.connectors.runtime.locks import _lock_key, _release
from tai42_skeleton.connectors.store.redis_pg import (
    _BLOB_FIELD,
    _CACHE_TOMBSTONE_LUA,
    _VER_FIELD,
    RedisPgConnectorTokenStore,
    StoredConnection,
)
from tai42_skeleton.utils.redis_typing import eval_script

from .conftest import CID
from .test_store_redis_pg import FakePg, _row


@pytest.fixture
async def lua_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[aioredis.FakeRedis]:
    """A Lua-executing fake Redis wired behind the store's + lock's client_ctx."""
    client = aioredis.FakeRedis()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield client

    monkeypatch.setattr(redis_pg, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(locks, "client_ctx", fake_client_ctx)
    try:
        yield client
    finally:
        await client.aclose()


def _rec_key(cid: str) -> str:
    return f"connectors:rec:{cid}"


# -- set-if-newer cache write (redis_pg._CACHE_SET_IF_NEWER_LUA) --------------


async def test_set_if_newer_installs_when_absent(lua_redis):
    store = RedisPgConnectorTokenStore()
    await store._cache_set(CID, b"cipher", None, 1)
    assert await lua_redis.hget(_rec_key(CID), _BLOB_FIELD) == b"cipher"
    assert await lua_redis.hget(_rec_key(CID), _VER_FIELD) == b"1"


async def test_set_if_newer_advances_on_newer_version(lua_redis):
    store = RedisPgConnectorTokenStore()
    await store._cache_set(CID, b"v1", None, 1)
    await store._cache_set(CID, b"v2", None, 2)
    assert await lua_redis.hget(_rec_key(CID), _BLOB_FIELD) == b"v2"
    assert await lua_redis.hget(_rec_key(CID), _VER_FIELD) == b"2"


async def test_set_if_newer_fences_out_older_version(lua_redis):
    """A stale late write-back holding an older version is a no-op — the cache
    never regresses below the latest durable write."""
    store = RedisPgConnectorTokenStore()
    await store._cache_set(CID, b"v5", None, 5)
    await store._cache_set(CID, b"stale", None, 3)
    assert await lua_redis.hget(_rec_key(CID), _BLOB_FIELD) == b"v5"
    assert await lua_redis.hget(_rec_key(CID), _VER_FIELD) == b"5"


async def test_set_if_newer_rejects_equal_version(lua_redis):
    """The fence is strict ``<``: an equal-version re-write does not overwrite."""
    store = RedisPgConnectorTokenStore()
    await store._cache_set(CID, b"first", None, 4)
    await store._cache_set(CID, b"second", None, 4)
    assert await lua_redis.hget(_rec_key(CID), _BLOB_FIELD) == b"first"


async def test_set_if_newer_persists_key_without_expiry(lua_redis):
    """No ``session_expires_at`` → the script PERSISTs the key (no TTL)."""
    store = RedisPgConnectorTokenStore()
    await store._cache_set(CID, b"cipher", None, 1)
    assert await lua_redis.ttl(_rec_key(CID)) == -1


async def test_set_if_newer_sets_expireat_with_session_expiry(lua_redis):
    store = RedisPgConnectorTokenStore()
    exp = datetime.now(UTC) + timedelta(hours=1)
    await store._cache_set(CID, b"cipher", exp, 1)
    assert await lua_redis.ttl(_rec_key(CID)) > 0


# -- delete tombstone (redis_pg._CACHE_TOMBSTONE_LUA) ------------------------
#
# ARGV mirrors the store's delete() call: KEYS[1]=record key, ARGV[1]=version
# field, ARGV[2]=deleted version, ARGV[3]=tombstone expireat.


def _future_expireat() -> int:
    return int((datetime.now(UTC) + timedelta(seconds=300)).timestamp())


async def test_tombstone_replaces_blob_with_version_marker(lua_redis):
    store = RedisPgConnectorTokenStore()
    key = _rec_key(CID)
    await store._cache_set(CID, b"live", None, 5)
    rv = await eval_script(lua_redis, _CACHE_TOMBSTONE_LUA, 1, key, _VER_FIELD, 5, _future_expireat())
    assert rv == 1
    # The blob is gone; only the version marker survives.
    assert await lua_redis.hget(key, _BLOB_FIELD) is None
    assert await lua_redis.hget(key, _VER_FIELD) == b"5"


async def test_tombstone_overwrites_equal_version_blob(lua_redis):
    """The fence is ``<=``: a tombstone at the same version a racing read just
    installed must still overwrite it (connection ids are never reused)."""
    store = RedisPgConnectorTokenStore()
    key = _rec_key(CID)
    await store._cache_set(CID, b"resurrected", None, 7)
    rv = await eval_script(lua_redis, _CACHE_TOMBSTONE_LUA, 1, key, _VER_FIELD, 7, _future_expireat())
    assert rv == 1
    assert await lua_redis.hget(key, _BLOB_FIELD) is None


async def test_tombstone_installs_when_absent(lua_redis):
    key = _rec_key(CID)
    rv = await eval_script(lua_redis, _CACHE_TOMBSTONE_LUA, 1, key, _VER_FIELD, 3, _future_expireat())
    assert rv == 1
    assert await lua_redis.hget(key, _VER_FIELD) == b"3"


async def test_tombstone_fences_out_older_version(lua_redis):
    """A tombstone carrying a version older than the cached one is a no-op — a
    newer durable write already superseded the delete."""
    store = RedisPgConnectorTokenStore()
    key = _rec_key(CID)
    await store._cache_set(CID, b"newer", None, 9)
    rv = await eval_script(lua_redis, _CACHE_TOMBSTONE_LUA, 1, key, _VER_FIELD, 4, _future_expireat())
    assert rv == 0
    assert await lua_redis.hget(key, _BLOB_FIELD) == b"newer"


async def test_tombstone_sets_ttl(lua_redis):
    key = _rec_key(CID)
    await eval_script(lua_redis, _CACHE_TOMBSTONE_LUA, 1, key, _VER_FIELD, 1, _future_expireat())
    assert await lua_redis.ttl(key) > 0


# -- lock release (runtime/locks._RELEASE_LUA) ------------------------------


async def test_release_deletes_only_when_token_matches(lua_redis):
    key = _lock_key(CID)
    await lua_redis.set(key, "our-token")
    await _release(CID, "our-token")
    assert await lua_redis.get(key) is None


async def test_release_leaves_lock_held_by_another_token(lua_redis):
    """A later holder acquired the lock after our PX expired — the compare-and-
    delete must not delete their lock."""
    key = _lock_key(CID)
    await lua_redis.set(key, "later-holder")
    await _release(CID, "our-token")
    assert await lua_redis.get(key) == b"later-holder"


# -- restore write-back against a racing read-populate ------------------------
#
# A read-populate is ``get``'s cache-miss path: it loads (blob, version) from
# Postgres and installs it through the set-if-newer script. The restore writes the
# row at V+1 and then installs (or tombstones) V+1. Whatever order the two take,
# the cache ends at V+1.


@pytest.fixture
async def restore_stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[aioredis.FakeRedis, FakePg]]:
    """Lua-executing Redis plus a stateful Postgres fake behind the store's client_ctx."""
    client = aioredis.FakeRedis()
    pg = FakePg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield pg if client_cls is PostgresClient else client

    monkeypatch.setattr(redis_pg, "client_ctx", fake_client_ctx)
    try:
        yield client, pg
    finally:
        await client.aclose()


def _record(blob: bytes, exp: datetime | None = None) -> StoredConnection:
    return StoredConnection(
        connection_id=CID, provider_id="acme", alias="work", encrypted_blob=blob, session_expires_at=exp
    )


async def _cached(client) -> tuple[bytes | None, bytes | None]:
    """The record's (blob, version) fields."""
    key = _rec_key(CID)
    return await client.hget(key, _BLOB_FIELD), await client.hget(key, _VER_FIELD)


async def test_refill_at_the_old_version_before_restore_is_overtaken(restore_stack):
    client, pg = restore_stack
    pg.rows[CID] = _row(b"old", ver=1)
    store = RedisPgConnectorTokenStore()
    await store._cache_set(CID, b"old", None, 1)
    await store.restore([_record(b"restored")], overwrite=True)
    assert await _cached(client) == (b"restored", b"2")


async def test_refill_at_the_old_version_after_restore_is_a_no_op(restore_stack):
    """A reader that loaded the pre-restore row installs it only after the restore's write-back."""
    client, pg = restore_stack
    pg.rows[CID] = _row(b"old", ver=1)
    store = RedisPgConnectorTokenStore()
    await store.restore([_record(b"restored")], overwrite=True)
    await store._cache_set(CID, b"old", None, 1)
    assert await _cached(client) == (b"restored", b"2")


async def test_refill_after_the_commit_and_before_the_write_back_holds(restore_stack, monkeypatch):
    """A reader that loads the restored row between the commit and the write-back installs V+1 itself."""
    client, pg = restore_stack
    pg.rows[CID] = _row(b"old", ver=1)
    store = RedisPgConnectorTokenStore()
    install = store._cache_install
    write_backs: list[int] = []

    async def refill_then_install(connection_id, blob, session_expires_at, version):
        if not write_backs:
            write_backs.append(version)
            await store.get(connection_id)  # the refill's own install goes straight to Redis
            assert await _cached(client) == (b"restored", b"2")
        await install(connection_id, blob, session_expires_at, version)

    monkeypatch.setattr(store, "_cache_install", refill_then_install)
    await store.restore([_record(b"restored")], overwrite=True)
    assert write_backs == [2]
    assert await _cached(client) == (b"restored", b"2")


async def test_warm_key_at_the_old_version_moves_to_the_restored_version(restore_stack):
    client, pg = restore_stack
    pg.rows[CID] = _row(b"old", ver=1)
    store = RedisPgConnectorTokenStore()
    await store.get(CID)
    assert await _cached(client) == (b"old", b"1")
    await store.restore([_record(b"restored")], overwrite=True)
    assert await _cached(client) == (b"restored", b"2")
    assert await store.get(CID) == b"restored"


async def test_expired_restore_tombstones_and_fences_a_late_refill(restore_stack):
    """An expired restored row leaves a tombstone at V+1; a refill holding V cannot re-install the old blob."""
    client, pg = restore_stack
    pg.rows[CID] = _row(b"old", ver=1)
    store = RedisPgConnectorTokenStore()
    await store.get(CID)
    past = datetime(2000, 1, 1, tzinfo=UTC)
    await store.restore([_record(b"lapsed", past)], overwrite=True)
    assert await _cached(client) == (None, b"2")
    assert await client.ttl(_rec_key(CID)) > 0
    await store._cache_set(CID, b"old", None, 1)
    assert await _cached(client) == (None, b"2")
    assert await store.get(CID) is None
