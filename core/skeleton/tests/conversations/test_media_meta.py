"""End-to-end execution of the inbound-media metadata store's Lua scripts (real ``fakeredis[lua]``).

Runs the REAL ``_MEDIA_PUT_LUA`` / ``_MEDIA_BIND_LUA`` text, so the atomic hash+expiry-index step
each carries and the bind ownership/expiry/bound-state checks are exercised, not a stand-in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fakeredis import aioredis
from redis.exceptions import ResponseError
from tai42_contract.interactions.models import MediaKind

from tai42_skeleton.conversations import media_meta as store_module
from tai42_skeleton.conversations.media_meta import _MEDIA_PUT_LUA, InboundMediaMetaStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.utils.redis_typing import eval_script


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


async def _put(store: InboundMediaMetaStore, media_id: str, *, pending: bool, message_id: str | None, expiry_at: float):
    await store.put(
        media_id,
        mime="image/png",
        size=42,
        sha256="abc",
        filename="pic.png",
        kind=MediaKind.IMAGE,
        storage_path=f"inbound-media/{media_id}",
        pending=pending,
        owner_channel_id="web",
        owner_participant_identity="sess-1",
        message_id=message_id,
        expiry_at=expiry_at,
    )


async def test_put_get_bind_delete_and_expiry_index(store: InboundMediaMetaStore, lua_redis: aioredis.FakeRedis):
    now = 1000.0
    await _put(store, "m1", pending=True, message_id=None, expiry_at=now + 900)

    meta = await store.get("m1")
    assert meta is not None
    assert meta.pending is True
    assert meta.size == 42
    assert meta.kind is MediaKind.IMAGE
    assert meta.message_id is None
    # The hash carries NO Redis TTL — the durable index alone drives retention.
    assert await lua_redis.pttl(store.meta_key("m1")) == -1
    assert await store.score("m1") == now + 900

    # bind flips pending and re-scores the expiry member to the record horizon.
    code = await store.bind(
        "m1", message_id="msg-A", channel_id="web", participant_identity="sess-1", now=now, expiry_at=now + 5000
    )
    assert code == 1
    rebound = await store.get("m1")
    assert rebound is not None
    assert rebound.pending is False
    assert rebound.message_id == "msg-A"
    assert await store.score("m1") == now + 5000

    # get/score on an absent id -> None; due_expiry_ids returns only members scored <= now.
    assert await store.get("absent") is None
    assert await store.score("absent") is None
    await _put(store, "past", pending=True, message_id=None, expiry_at=now - 1)
    due = await store.due_expiry_ids(now)
    assert "past" in due
    assert "m1" not in due

    # delete DELs the hash AND ZREMs the member.
    await store.delete("m1")
    assert await store.get("m1") is None
    assert await store.score("m1") is None


async def test_put_is_atomic_leaves_nothing_on_script_fault(
    store: InboundMediaMetaStore, lua_redis: aioredis.FakeRedis
):
    # A non-numeric expiry score aborts the whole script at its top guard, before any write.
    with pytest.raises(ResponseError):
        await eval_script(
            lua_redis,
            _MEDIA_PUT_LUA,
            2,
            store.meta_key("m2"),
            store.expiry_key,
            "image/png",
            "42",
            "abc",
            "pic.png",
            "image",
            "inbound-media/m2",
            "1",
            "web",
            "sess-1",
            "",
            "notanumber",
            "m2",
        )
    assert await lua_redis.exists(store.meta_key("m2")) == 0
    assert await store.score("m2") is None


async def test_bind_rescores_expiry_on_idempotent_same_message(store: InboundMediaMetaStore):
    now = 2000.0
    await _put(store, "m3", pending=True, message_id=None, expiry_at=now + 900)
    assert (
        await store.bind(
            "m3", message_id="A", channel_id="web", participant_identity="sess-1", now=now, expiry_at=now + 100
        )
        == 1
    )
    # A re-bind to the SAME message returns 0 AND re-scores (not a no-op).
    assert (
        await store.bind(
            "m3", message_id="A", channel_id="web", participant_identity="sess-1", now=now, expiry_at=now + 999
        )
        == 0
    )
    assert await store.score("m3") == now + 999
    # A bind to a DIFFERENT message conflicts, member unchanged.
    assert (
        await store.bind(
            "m3", message_id="B", channel_id="web", participant_identity="sess-1", now=now, expiry_at=now + 5
        )
        == -2
    )
    assert await store.score("m3") == now + 999


async def test_bind_codes_for_unknown_expired_notowned(store: InboundMediaMetaStore):
    now = 3000.0
    # Absent id.
    assert (
        await store.bind("nope", message_id="A", channel_id="web", participant_identity="s", now=now, expiry_at=now + 5)
        == -1
    )
    # Expired pending (score in the past).
    await _put(store, "old", pending=True, message_id=None, expiry_at=now - 1)
    assert (
        await store.bind(
            "old", message_id="A", channel_id="web", participant_identity="sess-1", now=now, expiry_at=now + 5
        )
        == -1
    )
    # Wrong owner.
    await _put(store, "owned", pending=True, message_id=None, expiry_at=now + 900)
    assert (
        await store.bind(
            "owned", message_id="A", channel_id="web", participant_identity="other", now=now, expiry_at=now + 5
        )
        == -1
    )
