"""The scripts that place a record in a person thread refuse a person that is marked or gone.

A send's completion, end and settle each delete a row bound for the thread of a marked or gone
person with every key it owns and answer -1; the create writes nothing and answers ``{-2, ''}``.
With the person row standing and unmarked, each places the record and, while the route row
stands, records the route in the person thread's recorded routes. The stores run the real script
text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations import record_write as record_write_module
from tai42_skeleton.conversations.models import (
    ConversationRecord,
    DeliveryStatus,
    PersonThreadClosedError,
    SentMessage,
)
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id

from .conftest import LuaEnv, _channel_route, keys_naming, past_deadline, standing_person

_PERSON = "15550002222"
_ADDRESS = f"+{_PERSON}"
_IDENTITY = "+15550001111"
_PERSON_THREAD = person_thread_id(_PERSON)
_ROUTE_THREAD = route_thread_id("line", _ADDRESS)


def _record(message_id: str, thread: str, **update) -> ConversationRecord:
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": "line",
        "door": "channel",
        "thread_id": thread,
        "client_address": _ADDRESS,
        "channel": "twilio",
        "our_identity": _IDENTITY,
        "origin": "send",
        "sent_by": "notify",
        "inbound_text": "",
        "answer_status": "answered",
        "answer": "hello",
        "delivery_status": DeliveryStatus.SENDING,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(update)
    return ConversationRecord(**fields)


async def _close(lua_env: LuaEnv, person: str) -> None:
    if person == "marked":
        assert await lua_env.manager.persons.mark_erasing(_PERSON) is True
    elif person == "gone":
        await lua_env.redis.delete(lua_env.store.settings.keyspace.person_key(_PERSON))


async def _status_members(lua_env: LuaEnv, message_id: str) -> list[str]:
    settings = lua_env.store.settings
    return [
        status.value
        for status in DeliveryStatus
        if await lua_env.redis.zscore(settings.keyspace.status_index_key(status.value), message_id) is not None
    ]


async def _end_one(lua_env: LuaEnv, end: str, record: ConversationRecord) -> tuple[int, str]:
    if end == "complete":
        done = record.model_copy(update={"sends": [SentMessage(part=0, chars=5, provider_message_ids=["out-1"])]})
        placement = await lua_env.store.complete_send_record(done, route=_channel_route())
        return placement.code, placement.detail
    if end == "end":
        placement = await lua_env.store.end_send_record(record, status=DeliveryStatus.FAILED, error="refused")
        return placement.code, placement.detail
    await past_deadline(lua_env, record.message_id)
    [settled] = await lua_env.store.settle_due_sends()
    return settled.placement.code, settled.placement.detail


@pytest.mark.parametrize("end", ["complete", "end", "settle"])
@pytest.mark.parametrize("person", ["marked", "gone"])
async def test_a_send_placed_in_a_closed_person_thread_is_deleted_with_every_key_it_owns(end, person, lua_env):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.seed_person(standing_person(_PERSON, "line"))
    record = _record("send-1", _PERSON_THREAD)
    await lua_env.store.begin_send_record(record, route=_channel_route(), settle_deadline=time.time() + 60)
    await _close(lua_env, person)

    assert await _end_one(lua_env, end, record) == (-1, "person_closed")

    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.keyspace.record_key("send-1")) == 0
    assert await keys_naming(lua_env, "send-1") == set()
    assert await _status_members(lua_env, "send-1") == []
    assert await lua_env.redis.exists(settings.keyspace.thread_index_key("line", _PERSON_THREAD)) == 0
    assert await lua_env.redis.zscore(settings.keyspace.route_threads_key("line"), _PERSON_THREAD) is None
    assert await lua_env.redis.exists(settings.keyspace.person_thread_routes_key(_PERSON)) == 0


@pytest.mark.parametrize("end", ["complete", "end", "settle"])
async def test_a_send_placed_in_a_standing_person_thread_lands_in_it(end, lua_env):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.seed_person(standing_person(_PERSON, "line"))
    record = _record("send-1", _PERSON_THREAD)
    await lua_env.store.begin_send_record(record, route=_channel_route(), settle_deadline=time.time() + 60)

    code, _detail = await _end_one(lua_env, end, record)

    settings = lua_env.store.settings
    assert code == 1
    assert await lua_env.redis.zrange(settings.keyspace.thread_index_key("line", _PERSON_THREAD), 0, -1) == ["send-1"]
    assert await lua_env.redis.zscore(settings.keyspace.route_threads_key("line"), _PERSON_THREAD) is not None
    assert await lua_env.redis.smembers(settings.keyspace.person_thread_routes_key(_PERSON)) == {"line"}


@pytest.mark.parametrize("end", ["complete", "end", "settle"])
async def test_a_send_placed_in_a_person_thread_with_its_route_gone_records_no_route(end, lua_env):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.seed_person(standing_person(_PERSON, "line"))
    record = _record("send-1", _PERSON_THREAD)
    await lua_env.store.begin_send_record(record, route=_channel_route(), settle_deadline=time.time() + 60)
    await lua_env.redis.delete(lua_env.store.settings.keyspace.route_key("line"))

    code, _detail = await _end_one(lua_env, end, record)

    assert code == 0
    assert await lua_env.redis.exists(lua_env.store.settings.keyspace.person_thread_routes_key(_PERSON)) == 0


@pytest.mark.parametrize("route_standing", [True, False])
async def test_a_create_in_a_standing_person_thread_records_its_route_while_the_route_stands(route_standing, lua_env):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.seed_person(standing_person(_PERSON, "line"))
    settings = lua_env.store.settings
    if not route_standing:
        await lua_env.redis.delete(settings.keyspace.route_key("line"))
    record = _record(
        "in-1",
        _PERSON_THREAD,
        origin="client",
        sent_by=None,
        inbound_text="hi",
        answer_status="answered",
        answer="ok",
        delivery_status=DeliveryStatus.DELIVERED,
    )

    await lua_env.store.create_record(record, route=_channel_route())

    recorded = await lua_env.redis.smembers(settings.keyspace.person_thread_routes_key(_PERSON))
    assert recorded == ({"line"} if route_standing else set())
    assert (
        await lua_env.redis.zscore(settings.keyspace.route_threads_key("line"), _PERSON_THREAD) is not None
    ) is route_standing


@pytest.mark.parametrize("person", ["marked", "gone"])
async def test_a_create_in_a_closed_person_thread_writes_nothing(person, lua_env):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.seed_person(standing_person(_PERSON, "line"))
    await _close(lua_env, person)
    settings = lua_env.store.settings
    record = _record(
        "in-1",
        _PERSON_THREAD,
        origin="client",
        sent_by=None,
        inbound_text="hi",
        inbound_message_id="medium-1",
        provider_message_id="PID1",
        answer_status=None,
        answer=None,
        delivery_status=DeliveryStatus.ACCEPTED,
    )

    with pytest.raises(PersonThreadClosedError, match=_PERSON_THREAD):
        await lua_env.store.create_record(
            record,
            route=_channel_route(),
            intake_token="tok",
            claim_key=settings.keyspace.dedupe_key("twilio", "PID1"),
            refresh_mode=True,
        )

    assert await lua_env.redis.exists(settings.keyspace.record_key("in-1")) == 0
    assert await lua_env.redis.exists(settings.keyspace.dedupe_key("twilio", "PID1")) == 0
    assert await lua_env.redis.exists(settings.keyspace.thread_index_key("line", _PERSON_THREAD)) == 0
    assert await lua_env.redis.zscore(settings.keyspace.route_threads_key("line"), _PERSON_THREAD) is None
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "medium-1") == []
    assert await _status_members(lua_env, "in-1") == []
    assert await lua_env.redis.exists(settings.keyspace.person_thread_routes_key(_PERSON)) == 0


async def test_a_create_in_a_route_keyed_thread_declares_no_person_key(lua_env, monkeypatch):
    settings = lua_env.store.settings
    declared: list[str] = []
    real = record_write_module.eval_script

    async def _spy(client, script, numkeys, *args):
        declared.extend(str(key) for key in args[:numkeys])
        return await real(client, script, numkeys, *args)

    monkeypatch.setattr(record_write_module, "eval_script", _spy)
    record = _record(
        "in-1",
        _ROUTE_THREAD,
        origin="client",
        sent_by=None,
        inbound_text="hi",
        answer_status="answered",
        answer="ok",
        delivery_status=DeliveryStatus.DELIVERED,
    )

    await lua_env.store.create_record(record, route=_channel_route())

    person_prefixes = (
        settings.keyspace.person_key_prefix,
        settings.keyspace.person_erasing_key_prefix,
        settings.keyspace.person_thread_routes_key_prefix,
    )
    assert declared
    assert [key for key in declared if key.startswith(person_prefixes)] == []
    assert [
        key async for key in lua_env.redis.scan_iter(match=f"{settings.keyspace.person_thread_routes_key_prefix}*")
    ] == []
