"""Bridge replies keep their sends and index each sent id once; a medium id resolves to its records.

A reply that fails part-way keeps the sends of the chunks already accepted. A resume re-runs the
chunk index for what the ledger already holds, and every writer re-run for the same record leaves
each message-ref list naming the record once and each key name on the row once. The message refs
resolve an inbound, a send part and a bridge reply part to their records, in write order. The stores
run the real script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_channel as delivery_channel_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.persons import thread_door
from tai42_skeleton.conversations.thread_ids import route_thread_id

from .conftest import LuaEnv, _channel_route, _FakeApp, create_record, owned_keys, sent_ids
from .test_delivery_channel import FakeChannel as ChunkChannel
from .test_delivery_channel import WorkerDiedError

_IDENTITY = "+15550001111"
_ADDRESS = "+15550002222"
_CHUNK_CHARS = 10


@pytest.fixture(autouse=True)
def _chunked(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", f'{{"twilio": {_CHUNK_CHARS}}}')


@pytest.fixture(autouse=True)
def _no_grace_timers(monkeypatch):
    async def _noop(message_id: str, grace_seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_module, "_confirm_after_grace", _noop)


@pytest.fixture(autouse=True)
def _drop_leftover_tasks():
    yield
    for task in list(delivery_module._DELIVERY_TASKS):
        task.cancel()
    delivery_module._DELIVERY_TASKS.clear()


def _wire(monkeypatch, channel) -> None:
    monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))


def _reply(message_id: str, answer: str, *, address: str = _ADDRESS, **update) -> ConversationRecord:
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": "line",
        "door": "channel",
        "thread_id": route_thread_id("line", address),
        "client_address": address,
        "channel": "twilio",
        "our_identity": _IDENTITY,
        "origin": "client",
        "inbound_text": f"ask {message_id}",
        "answer_status": "answered",
        "answer": answer,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(update)
    return ConversationRecord(**fields)


async def _get(lua_env: LuaEnv, message_id: str) -> ConversationRecord:
    record = await lua_env.store.get_record(message_id)
    assert record is not None
    return record


def _ref_key(lua_env: LuaEnv, medium_id: str, address: str = _ADDRESS) -> str:
    return lua_env.store.settings.keyspace.message_ref_key("twilio", _IDENTITY, address, medium_id)


async def _expire_claim(lua_env: LuaEnv, message_id: str) -> None:
    key = lua_env.store.settings.keyspace.record_key(message_id)
    token = (await lua_env.redis.hget(key, "claim")).split(":", 1)[0]
    await lua_env.redis.hset(key, "claim", f"{token}:{time.time() - 1}")


async def _claimed(lua_env: LuaEnv, message_id: str, token: str) -> ConversationRecord:
    assert await lua_env.store.claim_delivery(message_id, time.time(), token, 120) == 1
    return await _get(lua_env, message_id)


# -- bridge replies ---------------------------------------------------------------------------


async def test_a_reply_failing_part_way_keeps_the_sends_of_the_chunks_already_accepted(lua_env, monkeypatch):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-part", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    channel = ChunkChannel("w1", fail_on=2)
    _wire(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(lua_env.store, await _claimed(lua_env, "m-part", "t-1"), "t-1")

    record = await _get(lua_env, "m-part")
    assert record.delivery_status is DeliveryStatus.FAILED
    assert sent_ids(record) == ["w1-1"]
    assert channel.sends[:2] == ["aaaaaaaaaa", "bbbbbbbbbb"]


async def test_a_resumed_reply_reindexes_its_sent_chunk_once(lua_env, monkeypatch):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-resume", "aaaaaaaaaabbbbbbbbbb"))
    _wire(monkeypatch, ChunkChannel("w1", crash_on=2))
    with pytest.raises(WorkerDiedError):
        await delivery_channel_module._deliver_channel(lua_env.store, await _claimed(lua_env, "m-resume", "t-1"), "t-1")
    # The first chunk was sent, ledgered and indexed before the process stopped.
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "w1-1") == ["m-resume"]

    await _expire_claim(lua_env, "m-resume")
    resuming = ChunkChannel("w2")
    _wire(monkeypatch, resuming)
    await delivery_channel_module._deliver_channel(lua_env.store, await _claimed(lua_env, "m-resume", "t-2"), "t-2")

    assert resuming.sends == ["bbbbbbbbbb"]
    assert (await _get(lua_env, "m-resume")).delivery_status is DeliveryStatus.PROVISIONAL
    for medium_id in ("w1-1", "w2-1"):
        assert await lua_env.redis.lrange(_ref_key(lua_env, medium_id), 0, -1) == ["m-resume"]
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "w1-1") == ["m-resume"]
    owned = await owned_keys(lua_env, "m-resume")
    assert len(owned) == len(set(owned))
    settings = lua_env.store.settings
    assert set(owned) == {
        settings.keyspace.outbound_index_key("twilio", "w1-1"),
        settings.keyspace.outbound_index_key("twilio", "w2-1"),
        _ref_key(lua_env, "w1-1"),
        _ref_key(lua_env, "w2-1"),
    }


async def test_the_chunk_index_run_twice_names_the_record_once(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-twice", "hello"))

    for _ in range(2):
        assert await lua_env.store.index_outbound("twilio", _IDENTITY, _ADDRESS, ["o-1", "o-2"], "m-twice") == 1

    for medium_id in ("o-1", "o-2"):
        assert await lua_env.redis.lrange(_ref_key(lua_env, medium_id), 0, -1) == ["m-twice"]
    owned = await owned_keys(lua_env, "m-twice")
    assert len(owned) == 4
    assert len(owned) == len(set(owned))


async def test_a_create_re_run_under_the_same_id_names_the_record_once_and_stamps_one_writer(lua_env):
    route = _channel_route()
    await lua_env.manager.put_route(route)
    record = _reply("m-again", "hello", inbound_message_id="in-1", provider_message_id="ev-0")
    claim_key = lua_env.store.settings.keyspace.dedupe_key("twilio", "ev-0")
    thread = record.thread_id
    door = thread_door(route, thread)
    assert door is not None

    for _ in range(2):
        assert await create_record(lua_env.store, record, claim_key=claim_key) == "m-again"

    assert await lua_env.redis.lrange(_ref_key(lua_env, "in-1"), 0, -1) == ["m-again"]
    owned = await owned_keys(lua_env, "m-again")
    assert owned == [_ref_key(lua_env, "in-1")]
    settings = lua_env.store.settings
    assert await lua_env.redis.hget(settings.keyspace.record_key("m-again"), "writer") == door
    assert await lua_env.redis.smembers(settings.keyspace.writer_threads_key(door)) == {thread}


# -- refs and the store read --------------------------------------------------------------------


async def test_an_inbound_resolves_by_its_medium_id_and_a_miss_resolves_to_nothing(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-in", "hello", inbound_message_id="in-1"))

    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "in-1") == ["m-in"]
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "never-ours") == []


async def test_a_bridge_reply_part_resolves_to_its_record(lua_env, monkeypatch):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-bridge", "hello"))
    _wire(monkeypatch, ChunkChannel("w1"))

    await delivery_channel_module._deliver_channel(lua_env.store, await _claimed(lua_env, "m-bridge", "t-1"), "t-1")

    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "w1-1") == ["m-bridge"]


async def test_two_inbounds_carrying_one_medium_id_both_resolve_in_accept_order(lua_env):
    await lua_env.manager.put_route(_channel_route())
    first = _reply("m-first", "a", inbound_message_id="shared", provider_message_id="ev-0")
    second = _reply("m-second", "b", inbound_message_id="shared", provider_message_id="ev-1")
    await create_record(lua_env.store, first, claim_key=lua_env.store.settings.keyspace.dedupe_key("twilio", "ev-0"))
    await create_record(lua_env.store, second, claim_key=lua_env.store.settings.keyspace.dedupe_key("twilio", "ev-1"))
    resolve = lua_env.store.resolve_message_ref

    assert (await _get(lua_env, "m-first")).inbound_message_id == "shared"
    assert (await _get(lua_env, "m-second")).inbound_message_id == "shared"
    assert await resolve("twilio", _IDENTITY, _ADDRESS, "shared") == ["m-first", "m-second"]

    assert await lua_env.store.delete_record(await _get(lua_env, "m-first"))
    assert await resolve("twilio", _IDENTITY, _ADDRESS, "shared") == ["m-second"]
    assert await lua_env.redis.exists(_ref_key(lua_env, "shared")) == 1


async def test_an_inbound_past_its_retention_drops_out_of_the_shared_ref(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-first", "a", inbound_message_id="shared"))
    await create_record(lua_env.store, _reply("m-second", "b", inbound_message_id="shared"))

    # The retention TTL removes the row alone; the ref list keeps the member and the read skips it.
    await lua_env.redis.delete(lua_env.store.settings.keyspace.record_key("m-first"))

    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "shared") == ["m-second"]
    assert await lua_env.redis.exists(_ref_key(lua_env, "shared")) == 1


async def test_one_medium_id_under_two_addresses_resolves_to_two_records(lua_env):
    await lua_env.manager.put_route(_channel_route())
    other = "+15550003333"
    await create_record(lua_env.store, _reply("m-one", "a", inbound_message_id="same"))
    await create_record(lua_env.store, _reply("m-two", "b", address=other, inbound_message_id="same"))

    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "same") == ["m-one"]
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, other, "same") == ["m-two"]


async def test_the_refs_carry_the_retention_ttl_once_the_record_is_terminal(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _reply("m-ttl", "hello", inbound_message_id="in-1"))
    ref = _ref_key(lua_env, "in-1")
    await lua_env.redis.pexpire(ref, 5000)
    await _claimed(lua_env, "m-ttl", "t-1")

    assert await lua_env.store.mark_failed("m-ttl", 1, time.time(), "t-1", sends=[]) == 1

    retention_ms = lua_env.store.settings.answer_retention_ttl_seconds * 1000
    assert retention_ms - 60_000 < await lua_env.redis.pttl(ref) <= retention_ms
