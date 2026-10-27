"""Builders and reads for the route-keyed thread writer script tests.

The writes run the real store steps on ``fakeredis[lua]`` (the ``lua_env`` fixture): a create, a
send's begin and end, and the person store's steps. The reads take what the scripts keep: the
writers of a thread's standing records and a writer's visit list.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

from tai42_contract.conversations import ConversationRoute, Person, PersonAddress

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus, SentMessage
from tai42_skeleton.conversations.record_send import SendPlacement
from tai42_skeleton.conversations.thread_ids import route_thread_id

from .conftest import LuaEnv, _channel_route, past_deadline

IDENTITY = "+15550001111"
OTHER_IDENTITY = "+15550001112"
A, B, C = "+15550002001", "+15550002002", "+15550002003"
ROUTE = _channel_route()
T_A = route_thread_id("line", A)
T_B = route_thread_id("line", B)
NOW = datetime.now(UTC)


def dak(address: str, identity: str = IDENTITY) -> str:
    """The door address key of a channel address on the line's channel."""
    return json.dumps(["twilio", identity, address], separators=(",", ":"))


def door(address: str, identity: str = IDENTITY, target_name: str = "echo") -> str:
    """The door a write by ``address`` under ``identity`` stamps while nobody holds it."""
    return json.dumps(["agent", target_name, dak(address, identity)], separators=(",", ":"))


def inbound(message_id: str, thread: str, address: str = A, **update) -> ConversationRecord:
    """An answered client inbound by ``address`` into ``thread`` on the line."""
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": "line",
        "door": "channel",
        "thread_id": thread,
        "client_address": address,
        "channel": "twilio",
        "our_identity": IDENTITY,
        "origin": "client",
        "inbound_text": "hi",
        "answer_status": "answered",
        "answer": "ok",
        "delivery_status": DeliveryStatus.DELIVERED,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(update)
    return ConversationRecord(**fields)


def send(message_id: str, thread: str, address: str = A) -> ConversationRecord:
    """A send to ``address`` begun into ``thread`` on the line."""
    return inbound(
        message_id,
        thread,
        address,
        origin="send",
        sent_by="notify",
        inbound_text="",
        answer="hello",
        delivery_status=DeliveryStatus.SENDING,
    )


async def create(lua_env: LuaEnv, record: ConversationRecord, *, route: ConversationRoute = ROUTE, **kwargs) -> str:
    """Create ``record`` under ``route``."""
    return await lua_env.store.create_record(record, route=route, **kwargs)


async def begin(lua_env: LuaEnv, record: ConversationRecord) -> None:
    """Begin ``record`` on the line."""
    await lua_env.store.begin_send_record(record, route=ROUTE, settle_deadline=time.time() + 60)


async def end_send(lua_env: LuaEnv, how: str, record: ConversationRecord) -> SendPlacement:
    """End a begun send by its completion, its failed end or its settle."""
    if how == "complete":
        done = record.model_copy(update={"sends": [SentMessage(part=0, chars=5, provider_message_ids=["out-1"])]})
        return await lua_env.store.complete_send_record(done, route=ROUTE)
    if how == "end":
        return await lua_env.store.end_send_record(record, status=DeliveryStatus.FAILED, error="refused")
    await past_deadline(lua_env, record.message_id)
    [settled] = await lua_env.store.settle_due_sends()
    return settled.placement


def channel_address(address: str, identity: str = IDENTITY, *routes: str) -> PersonAddress:
    """A linked channel address of the line's channel."""
    return PersonAddress(
        door="channel",
        channel="twilio",
        our_identity=identity,
        address=address,
        routes=list(routes or ("line",)),
        linked_at=NOW,
    )


async def seed(
    lua_env: LuaEnv, person_id: str, *addresses: PersonAddress, age_days: int = 0, target_name: str = "echo"
) -> Person:
    """Plant a person of the agent ``target_name`` with ``addresses``, created ``age_days`` ago."""
    person = Person(
        person_id=person_id,
        target_kind="agent",
        target_name=target_name,
        created_at=NOW - timedelta(days=age_days),
        addresses=list(addresses),
    )
    await lua_env.seed_person(person)
    return person


async def writers(lua_env: LuaEnv, thread: str = T_A) -> set[str]:
    """The writers of the records standing in route-keyed ``thread``."""
    settings = lua_env.store.settings
    route = thread.removeprefix("bridge:").split(":", 1)[0]
    found: set[str] = set()
    for message_id in await lua_env.redis.zrange(settings.keyspace.thread_index_key(route, thread), 0, -1):
        writer = await lua_env.redis.hget(settings.keyspace.record_key(message_id), "writer")
        if writer is not None:
            found.add(writer)
    return found


async def threads_of(lua_env: LuaEnv, writer: str) -> set[str]:
    """The threads on ``writer``'s visit list."""
    return await lua_env.redis.smembers(lua_env.store.settings.keyspace.writer_threads_key(writer))


async def stamps(lua_env: LuaEnv) -> dict[str, set[str]]:
    """Every writer's visit list standing, by key name."""
    prefix = lua_env.store.settings.keyspace.writer_threads_key_prefix
    return {key: await lua_env.redis.smembers(key) async for key in lua_env.redis.scan_iter(match=f"{prefix}*")}


async def row_field(lua_env: LuaEnv, message_id: str, field: str) -> str | None:
    """One field of a record row."""
    return await lua_env.redis.hget(lua_env.store.settings.keyspace.record_key(message_id), field)


async def point(lua_env: LuaEnv, absorbed_id: str, survivor_id: str) -> None:
    """Plant an absorbed id's absorbed-into pointer."""
    await lua_env.redis.set(lua_env.store.settings.keyspace.person_absorbed_into_key(absorbed_id), survivor_id)


async def absorbed(lua_env: LuaEnv, person_id: str) -> set[str]:
    """The ids a person absorbed."""
    return await lua_env.redis.smembers(lua_env.store.settings.keyspace.person_absorbed_key(person_id))


async def into(lua_env: LuaEnv, absorbed_id: str) -> str | None:
    """An absorbed id's absorbed-into pointer."""
    return await lua_env.redis.get(lua_env.store.settings.keyspace.person_absorbed_into_key(absorbed_id))
