"""End-to-end execution of the per-target config store's Redis Lua scripts.

``test_target_config_store`` drives a Python re-implementation of ``_PUT_LUA`` / ``_DELETE_LUA``
(``fake_config_redis``); this module runs the REAL script text against ``fakeredis[lua]``, so
the atomic name-index step each one carries is exercised, not just a stand-in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fakeredis import aioredis
from tai42_contract.conversations import TargetConversationConfig

from tai42_skeleton.conversations import target_config as store_module
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore, _member


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")


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
def store(lua_redis: aioredis.FakeRedis) -> ConversationTargetConfigStore:
    return ConversationTargetConfigStore(ConversationsSettings())


async def test_upsert_keys_and_indexes_the_row_atomically(store, lua_redis):
    # A fresh create returns True and the SADD indexes the member; a replace returns False and
    # leaves exactly one index member.
    settings = store.settings
    assert await store.upsert(TargetConversationConfig(target_kind="agent", target_name="assistant")) is True
    assert await lua_redis.exists(settings.target_config_key("agent", "assistant")) == 1
    assert await lua_redis.smembers(settings.target_config_names_key) == {_member("agent", "assistant")}

    assert (
        await store.upsert(TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True))
        is False
    )
    assert await lua_redis.smembers(settings.target_config_names_key) == {_member("agent", "assistant")}
    got = await store.get("agent", "assistant")
    assert got is not None
    assert got.multichannel is True


async def test_delete_removes_the_row_and_unindexes_it_atomically(store, lua_redis):
    settings = store.settings
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="assistant"))

    assert await store.delete("agent", "assistant") is True
    assert await lua_redis.exists(settings.target_config_key("agent", "assistant")) == 0
    assert _member("agent", "assistant") not in await lua_redis.smembers(settings.target_config_names_key)
    assert await lua_redis.smembers(settings.target_config_names_key) == set()
    # A second delete removes nothing and says so, and the SREM is harmless on an absent member.
    assert await store.delete("agent", "assistant") is False
