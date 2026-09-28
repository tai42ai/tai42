"""The inbound-media retention reaper — the SOLE deleter of an ingested blob + its metadata.

Drives the REAL ``InboundMediaMetaStore`` Lua over ``fakeredis[lua]`` and a fake blob provider, so
the delete ORDER (blob → meta hash → expiry member), the ``FileNotFoundError``-maps-to-proceed
branch, the per-member store-fault branch (member kept + evented), the re-scored-survives case, the
sole-deleter invariant (no hash TTL), the unconfigured no-op, and the dead-task readiness wiring are
all exercised against the shipped code, not a stand-in.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis
from tai42_contract.interactions.models import MediaKind

from tai42_skeleton.conversations import media_meta as store_module
from tai42_skeleton.conversations import media_reaper
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore
from tai42_skeleton.conversations.settings import ConversationsSettings


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


@pytest.fixture
async def lua_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[aioredis.FakeRedis]:
    client = aioredis.FakeRedis(decode_responses=True)

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield client

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def store(lua_redis: aioredis.FakeRedis) -> InboundMediaMetaStore:
    return InboundMediaMetaStore(ConversationsSettings())


class _FakeProvider:
    """A blob provider whose ``delete`` raises ``FileNotFoundError`` on a miss, like every implementer."""

    def __init__(self, existing: set[str]) -> None:
        self.existing = set(existing)
        self.deleted: list[str] = []
        self.fault_on: set[str] = set()

    async def delete(self, path: str) -> None:
        if path in self.fault_on:
            raise RuntimeError("store connection error")
        if path not in self.existing:
            raise FileNotFoundError(path)
        self.existing.discard(path)
        self.deleted.append(path)


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider | None) -> None:
    monkeypatch.setattr(media_reaper, "_storage_provider", lambda: provider)


def _blob(media_id: str) -> str:
    return f"inbound-media/{media_id}"


async def _put(
    store: InboundMediaMetaStore,
    media_id: str,
    *,
    pending: bool,
    message_id: str | None,
    expiry_at: float,
) -> None:
    await store.put(
        media_id,
        mime="image/png",
        size=42,
        sha256="abc",
        filename="pic.png",
        kind=MediaKind.IMAGE,
        storage_path=_blob(media_id),
        pending=pending,
        owner_channel_id="web",
        owner_participant_identity="sess-1",
        message_id=message_id,
        expiry_at=expiry_at,
    )


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch):
    """Pin the reaper's ``time.time()`` so a due/not-due horizon is deterministic."""

    def _set(value: float) -> None:
        monkeypatch.setattr(media_reaper, "time", SimpleNamespace(time=lambda: value))

    return _set


async def test_reaps_expired_bound_blob_and_meta(store, lua_redis, monkeypatch, fixed_now):
    fixed_now(2000.0)
    provider = _FakeProvider({_blob("due"), _blob("live")})
    _install_provider(monkeypatch, provider)
    await _put(store, "due", pending=False, message_id="msg-1", expiry_at=1000.0)
    await _put(store, "live", pending=False, message_id="msg-2", expiry_at=9000.0)

    reaped = await media_reaper.reap_expired_media_once()

    assert reaped == 1
    assert provider.deleted == [_blob("due")]
    assert await store.get("due") is None
    assert await store.score("due") is None
    # The not-yet-due member is untouched: blob, hash and expiry member all survive.
    assert _blob("live") in provider.existing
    assert await store.get("live") is not None
    assert await store.score("live") == 9000.0


async def test_reaps_expired_bound_when_meta_hash_already_gone(store, lua_redis, monkeypatch, fixed_now):
    # The member alone names the blob — a hash already gone (a re-driven crash) still reclaims the blob.
    fixed_now(2000.0)
    provider = _FakeProvider({_blob("due")})
    _install_provider(monkeypatch, provider)
    await _put(store, "due", pending=False, message_id="msg-1", expiry_at=1000.0)
    await lua_redis.delete(store.meta_key("due"))  # simulate the hash gone but the member remaining

    reaped = await media_reaper.reap_expired_media_once()

    assert reaped == 1
    assert provider.deleted == [_blob("due")]
    assert await store.score("due") is None


async def test_media_reaper_reaps_abandoned_pending(store, lua_redis, monkeypatch, fixed_now):
    fixed_now(2000.0)
    provider = _FakeProvider({_blob("abandoned"), _blob("fresh")})
    _install_provider(monkeypatch, provider)
    await _put(store, "abandoned", pending=True, message_id=None, expiry_at=1000.0)
    await _put(store, "fresh", pending=True, message_id=None, expiry_at=9000.0)

    reaped = await media_reaper.reap_expired_media_once()

    assert reaped == 1
    assert provider.deleted == [_blob("abandoned")]
    assert await store.get("abandoned") is None
    # A pending upload still inside its window is untouched.
    assert await store.get("fresh") is not None
    assert _blob("fresh") in provider.existing


async def test_media_reaper_bound_rescored_survives_pending_horizon(store, lua_redis, monkeypatch, fixed_now):
    # A pending member re-scored to the record horizon by bind is NOT reaped at a pass past the OLD
    # pending horizon but before the record horizon — the re-score moved it out of the due window.
    provider = _FakeProvider({_blob("rescored")})
    _install_provider(monkeypatch, provider)
    await _put(store, "rescored", pending=True, message_id=None, expiry_at=3000.0)
    code = await store.bind(
        "rescored",
        message_id="msg-1",
        channel_id="web",
        participant_identity="sess-1",
        now=1500.0,
        expiry_at=5000.0,
    )
    assert code == 1

    fixed_now(4000.0)  # past the old pending horizon (3000), before the record horizon (5000)
    reaped = await media_reaper.reap_expired_media_once()

    assert reaped == 0
    assert provider.deleted == []
    assert await store.get("rescored") is not None
    assert await store.score("rescored") == 5000.0


async def test_media_reaper_crash_mid_delete_is_redriven(store, lua_redis, monkeypatch, fixed_now):
    # A member whose blob was already deleted (a crash after the blob delete): the provider raises
    # FileNotFoundError, the reaper maps it to proceed and completes the delete with no error.
    fixed_now(2000.0)
    provider = _FakeProvider(set())  # the blob is absent
    _install_provider(monkeypatch, provider)
    await _put(store, "half", pending=False, message_id="msg-1", expiry_at=1000.0)

    reaped = await media_reaper.reap_expired_media_once()

    assert reaped == 1
    assert provider.deleted == []  # nothing to delete; the miss was mapped to proceed
    assert await store.get("half") is None
    assert await store.score("half") is None

    # A member whose hash was deleted but not yet ZREM'd is likewise re-driven off the bare member.
    await _put(store, "orphan", pending=False, message_id="msg-2", expiry_at=1000.0)
    await lua_redis.delete(store.meta_key("orphan"))
    reaped = await media_reaper.reap_expired_media_once()
    assert reaped == 1
    assert await store.score("orphan") is None


async def test_reaper_maps_missing_blob_to_proceed(store, lua_redis, monkeypatch, fixed_now):
    # The record-first crash/upload-fault case: the hash + member exist but the blob was never
    # written. The provider raises FileNotFoundError; the reaper reclaims the record cleanly.
    fixed_now(2000.0)
    provider = _FakeProvider(set())
    _install_provider(monkeypatch, provider)
    await _put(store, "noblob", pending=False, message_id="msg-1", expiry_at=1000.0)

    reaped = await media_reaper.reap_expired_media_once()

    assert reaped == 1
    assert await store.get("noblob") is None
    assert await store.score("noblob") is None


async def test_media_reaper_is_sole_deleter(store, lua_redis, monkeypatch, fixed_now):
    # No hash TTL and no put/bind delete: only a reaper pass removes the blob/meta/member.
    fixed_now(2000.0)
    provider = _FakeProvider({_blob("kept"), _blob("due")})
    _install_provider(monkeypatch, provider)
    await _put(store, "kept", pending=False, message_id="msg-1", expiry_at=9000.0)

    # The meta hash carries no Redis TTL — the durable index is the enumerator, not a hash expiry.
    assert await lua_redis.pttl(store.meta_key("kept")) == -1

    # A pass over a surface with nothing due deletes nothing.
    assert await media_reaper.reap_expired_media_once() == 0
    assert await store.get("kept") is not None
    assert _blob("kept") in provider.existing

    # Only when a member is past its horizon does the reaper delete it.
    await _put(store, "due", pending=False, message_id="msg-2", expiry_at=1000.0)
    assert await media_reaper.reap_expired_media_once() == 1
    assert await store.get("due") is None
    assert await store.get("kept") is not None


async def test_media_reaper_per_member_fault_left_for_next_pass(store, lua_redis, monkeypatch, fixed_now):
    fixed_now(2000.0)
    provider = _FakeProvider({_blob("bad"), _blob("good")})
    provider.fault_on = {_blob("bad")}
    _install_provider(monkeypatch, provider)
    events: list[tuple[str, dict]] = []
    hooks = SimpleNamespace(on_event=AsyncMock(side_effect=lambda *, topic, payload: events.append((topic, payload))))
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: hooks)
    await _put(store, "bad", pending=False, message_id="msg-1", expiry_at=1000.0)
    await _put(store, "good", pending=False, message_id="msg-2", expiry_at=1000.0)

    reaped = await media_reaper.reap_expired_media_once()

    # The faulting member stays for the next pass; the other is still reaped (a defined retry).
    assert reaped == 1
    assert await store.get("bad") is not None
    assert await store.score("bad") == 1000.0
    assert await store.get("good") is None
    # The fault is evented, carrying only the media id.
    assert events == [(media_reaper.MEDIA_REAP_FAILED_EVENT_TOPIC, {"media_id": "bad"})]


async def test_reaper_store_fault_on_delete_leaves_member_for_next_pass(store, lua_redis, monkeypatch, fixed_now):
    # Branch (b): a NON-FileNotFoundError store fault keeps the member (hash + expiry member), a
    # subsequent pass retries; branch (a) FileNotFoundError proceeds to delete. Exact distinction.
    fixed_now(2000.0)
    provider = _FakeProvider({_blob("stuck")})
    provider.fault_on = {_blob("stuck")}
    _install_provider(monkeypatch, provider)
    hooks = SimpleNamespace(on_event=AsyncMock())
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: hooks)
    await _put(store, "stuck", pending=False, message_id="msg-1", expiry_at=1000.0)

    assert await media_reaper.reap_expired_media_once() == 0
    assert await store.get("stuck") is not None
    assert await store.score("stuck") == 1000.0

    # The store fault clears; the next pass now maps the (still-present) blob delete to a success.
    provider.fault_on = set()
    assert await media_reaper.reap_expired_media_once() == 1
    assert await store.get("stuck") is None


async def test_media_reaper_scan_fault_propagates(store, lua_redis, monkeypatch, fixed_now):
    # A whole-scan fault (the index read itself) is NOT swallowed — it propagates out of the pass.
    fixed_now(2000.0)
    _install_provider(monkeypatch, _FakeProvider(set()))

    async def _boom(self, now, *, limit=500):
        raise RuntimeError("zrangebyscore failed")

    monkeypatch.setattr(InboundMediaMetaStore, "due_expiry_ids", _boom)
    with pytest.raises(RuntimeError, match="zrangebyscore failed"):
        await media_reaper.reap_expired_media_once()


async def test_media_reaper_noop_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # No conversations store (no redis) → a no-op, no raise, no provider consulted.
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    provider = _FakeProvider({"inbound-media/x"})
    _install_provider(monkeypatch, provider)
    assert await media_reaper.reap_expired_media_once() == 0
    assert provider.deleted == []


async def test_media_reaper_noop_when_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # A configured store but no blob provider registered → a no-op, no raise.
    _install_provider(monkeypatch, None)
    assert await media_reaper.reap_expired_media_once() == 0


async def test_media_reaper_dead_task_marks_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reaper registers the SAME _on_perpetual_task_done seam as the other perpetual tasks, so a
    # death (here a normal return) marks readiness named by its task and requests a graceful exit.
    from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin, lifespan

    class _M(TaiMCPLifecycleMixin):
        def _mcp_tools(self, config, tools):  # pragma: no cover - unused here
            self._mcp_bound_tools[config.title] = set()

    m = _M()
    exits: list[object] = []
    monkeypatch.setattr(lifespan, "graceful_exit_for", lambda kind: lambda: exits.append(kind))

    async def _returns_immediately() -> None:
        return None

    monkeypatch.setattr(media_reaper, "run_media_reaper_loop", _returns_immediately)
    m._spawn_media_reaper()
    task = m._media_reaper_task
    assert task is not None
    await task
    await asyncio.sleep(0)  # let the done-callback run

    assert m._dead_perpetual_task == ("tai-media-retention-reaper", "returned")
    assert exits == [m._worker_kind]
