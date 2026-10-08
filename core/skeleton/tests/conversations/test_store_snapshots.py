"""The route table and the target-config map served from a per-process snapshot.

Runs the REAL write scripts against ``fakeredis[lua]``: each script sets its store's version key
to a fresh token in the same atomic unit as its write, and a reader serves its snapshot only
while the token it reads back equals the one the snapshot was loaded under.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_contract.conversations import ConversationRoute, TargetConversationConfig

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import target_config as target_config_module
from tai42_skeleton.conversations.managers import base_conversations_manager as base_module
from tai42_skeleton.conversations.managers import redis_conversations_manager as redis_module
from tai42_skeleton.conversations.managers.redis_conversations_manager import RedisConversationsManager
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


class _CountingRedis(aioredis.FakeRedis):
    """A Lua-executing fake that records the name of every command it is sent."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[str] = []
        self.fail_mget = False

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        name = str(args[0]).upper()
        self.commands.append(name)
        if name == "MGET" and self.fail_mget:
            raise ConnectionError("mget failed")
        return await super().execute_command(*args, **options)


@pytest.fixture
async def lua_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_CountingRedis]:
    client = _CountingRedis(decode_responses=True)

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield client

    monkeypatch.setattr(redis_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(target_config_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(records_module, "client_ctx", fake_client_ctx)
    try:
        yield client
    finally:
        await client.aclose()


def _manager() -> RedisConversationsManager:
    return RedisConversationsManager(ConversationsSettings())


def _route(name: str = "chat", **over: Any) -> ConversationRoute:
    fields: dict[str, Any] = {
        "route_name": name,
        "door": "api",
        "target_kind": "agent",
        "target_name": "relay",
        "execution_key": "svc",
        "callback_url": "https://example.com/cb",
        "execution_key_fingerprint": "fp-1",
        "callback_secret": "sec-1",
    }
    fields.update(over)
    return ConversationRoute(**fields)


def _config(name: str = "assistant", **over: Any) -> TargetConversationConfig:
    return TargetConversationConfig(target_kind="agent", target_name=name, **over)


def _s() -> ConversationsSettings:
    return ConversationsSettings()


# -- each write script sets its store's token ----------------------------------------------


async def test_every_route_write_sets_a_fresh_token(lua_redis):
    manager = _manager()
    assert await lua_redis.get(_s().route_version_key) is None

    await manager.put_route(_route())
    after_put = await lua_redis.get(_s().route_version_key)
    assert after_put is not None

    await manager.put_route(_route(target_name="other"))
    after_replace = await lua_redis.get(_s().route_version_key)
    assert after_replace not in (None, after_put)

    await manager.delete_route("chat")
    after_delete = await lua_redis.get(_s().route_version_key)
    assert after_delete not in (None, after_replace)

    # A delete of an absent row still sets a token.
    await manager.delete_route("chat")
    assert await lua_redis.get(_s().route_version_key) not in (None, after_delete)


async def test_a_refused_door_flip_writes_nothing_and_sets_no_token(lua_redis):
    manager = _manager()
    await manager.put_route(_route())
    token = await lua_redis.get(_s().route_version_key)
    await lua_redis.zadd(_s().route_threads_key("chat"), {"t1": 1.0})

    with pytest.raises(base_module.DoorFlipRefusedError):
        await manager.put_route(_route(door="channel", channel="web", our_identity="me", callback_url=None))

    assert await lua_redis.get(_s().route_version_key) == token


async def test_every_config_write_sets_a_fresh_token(lua_redis):
    store = ConversationTargetConfigStore(_s())
    assert await lua_redis.get(_s().target_config_version_key) is None

    await store.upsert(_config())
    after_put = await lua_redis.get(_s().target_config_version_key)
    assert after_put is not None

    await store.delete("agent", "assistant")
    assert await lua_redis.get(_s().target_config_version_key) not in (None, after_put)


# -- the snapshot read ------------------------------------------------------------------------


async def test_a_write_through_one_process_is_seen_by_the_others_next_read(lua_redis):
    reader, writer = _manager(), _manager()
    await writer.put_route(_route("a"))
    assert set((await reader.list_routes())[0]) == {"a"}

    await writer.put_route(_route("b"))
    assert set((await reader.list_routes())[0]) == {"a", "b"}
    assert await reader.get_route("b") == _route("b")

    await writer.delete_route("a")
    assert set((await reader.list_routes())[0]) == {"b"}
    assert await reader.get_route("a") is None


async def test_a_config_write_through_one_process_is_seen_by_the_others_next_read(lua_redis):
    reader, writer = ConversationTargetConfigStore(_s()), ConversationTargetConfigStore(_s())
    await writer.upsert(_config())
    assert (await reader.list())[0] == {("agent", "assistant"): _config()}

    await writer.upsert(_config(multichannel=True))
    got = await reader.get("agent", "assistant")
    assert got is not None
    assert got.multichannel is True

    await writer.delete("agent", "assistant")
    assert await reader.get("agent", "assistant") is None
    assert (await reader.list())[0] == {}


async def test_a_flush_makes_the_next_read_reload(lua_redis):
    reader = _manager()
    await reader.put_route(_route())
    assert set((await reader.list_routes())[0]) == {"chat"}

    await lua_redis.flushall()

    assert (await reader.list_routes()) == ({}, 0)
    assert await reader.get_route("chat") is None


async def test_a_flush_and_the_same_number_of_writes_never_match_the_old_snapshot(lua_redis):
    reader, writer = _manager(), _manager()
    await writer.put_route(_route("first"))
    assert set((await reader.list_routes())[0]) == {"first"}

    await lua_redis.flushall()
    await writer.put_route(_route("second"))

    assert set((await reader.list_routes())[0]) == {"second"}


async def test_a_list_after_a_hit_reads_only_the_token(lua_redis):
    manager = _manager()
    await manager.put_route(_route())
    await manager.list_routes()

    lua_redis.commands.clear()
    routes, unreadable = await manager.list_routes()
    assert set(routes) == {"chat"}
    assert unreadable == 0
    assert lua_redis.commands == ["GET"]

    lua_redis.commands.clear()
    assert await manager.get_route("chat") == _route()
    assert await manager.get_route("absent") is None
    assert lua_redis.commands == ["GET", "GET"]


async def test_a_config_read_after_a_hit_reads_only_the_token(lua_redis):
    store = ConversationTargetConfigStore(_s())
    await store.upsert(_config())
    await store.list()

    lua_redis.commands.clear()
    assert await store.get("agent", "assistant") == _config()
    assert lua_redis.commands == ["GET"]


async def test_unreadable_members_are_counted_and_read_live(lua_redis):
    manager = _manager()
    await manager.put_route(_route())
    # Hand-written corruption under the store's keys; the version key moved with it.
    await lua_redis.sadd(_s().route_names_key, "ghost", "broken")
    await lua_redis.set(_s().route_key("broken"), "{not json")
    await lua_redis.set(_s().route_version_key, "hand-written")

    routes, unreadable = await manager.list_routes()
    assert set(routes) == {"chat"}
    assert unreadable == 2
    assert await manager.get_route("ghost") is None
    with pytest.raises(ValueError, match="validation error"):
        await manager.get_route("broken")

    # The live read of an unreadable member follows the row as it is now.
    await lua_redis.set(_s().route_key("broken"), _route("broken").model_dump_json())
    assert await manager.get_route("broken") == _route("broken")


async def test_unreadable_config_members_are_counted_and_read_live(lua_redis):
    store = ConversationTargetConfigStore(_s())
    await store.upsert(_config())
    await lua_redis.sadd(_s().target_config_names_key, "agent:broken")
    await lua_redis.set(_s().target_config_key("agent", "broken"), "{not json")
    await lua_redis.set(_s().target_config_version_key, "hand-written")

    configs, unreadable = await store.list()
    assert set(configs) == {("agent", "assistant")}
    assert unreadable == 1
    with pytest.raises(ValueError, match="validation error"):
        await store.get("agent", "broken")


async def test_a_failing_load_stores_nothing_and_the_next_read_loads_again(lua_redis):
    manager = _manager()
    await manager.put_route(_route())

    lua_redis.fail_mget = True
    with pytest.raises(ConnectionError, match="mget failed"):
        await manager.list_routes()
    lua_redis.fail_mget = False

    lua_redis.commands.clear()
    assert set((await manager.list_routes())[0]) == {"chat"}
    assert "MGET" in lua_redis.commands


async def test_a_deleted_route_is_refused_by_the_record_create_while_a_reader_holds_the_old_snapshot(lua_redis):
    reader, writer = _manager(), _manager()
    await writer.put_route(_route("line"))
    assert await reader.get_route("line") is not None

    await writer.delete_route("line")

    # The record create decides inside Redis whether the route still routes, so no snapshot
    # can admit a record against the deleted route.
    now = time.time()
    record = ConversationRecord(
        message_id="m1",
        route_name="line",
        door="channel",
        thread_id="bridge:line:m1",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id="PID-m1",
        origin="client",
        inbound_text="hello",
        delivery_status=DeliveryStatus.PENDING_DELIVERY,
        answer_status="answered",
        answer="the answer",
        created_at=now,
        updated_at=now,
    )
    await ConversationRecordStore(_s()).create_record(record)
    assert await lua_redis.zcard(_s().route_threads_key("line")) == 0
