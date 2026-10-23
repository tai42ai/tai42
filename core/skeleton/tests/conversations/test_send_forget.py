"""Forgetting a thread takes every key that names its records, including a send still going out.

Each record row names the keys outside it that name the record (``owned_keys``), and the one
delete script removes them with the row. A send still going out when its thread is forgotten is
marked through its route's in-flight set and forgotten when it ends in that thread; one that lands
elsewhere is recorded where it lands. Every assertion is a scan of the store's keys by prefix. The
stores run the real script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import pytest
from tai42_contract.channels import ChannelDeliveryError, SendOutcome
from tai42_contract.conversations import Person, TargetConversationConfig

from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_channel as delivery_channel_module
from tai42_skeleton.conversations import sends as sends_module
from tai42_skeleton.conversations.models import AskReply, AskSend, ConversationRecord, DeliveryStatus, SentMessage
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import NotFoundError

from .conftest import (
    FakeChannel,
    GatedChannel,
    LuaEnv,
    RaisingGatedChannel,
    _channel_route,
    _FakeApp,
    create_record,
    keys_naming,
    owned_keys,
    past_deadline,
    send_records,
    standing_person,
)
from .test_delivery_channel import FakeChannel as ChunkChannel

_IDENTITY = "+15550001111"
_ADDRESS = "+15550002222"
_THREAD = route_thread_id("line", _ADDRESS)
_PERSON = "15550002222"
_PERSON_THREAD = person_thread_id(_PERSON)


@pytest.fixture(autouse=True)
def _no_grace_confirms(monkeypatch):
    monkeypatch.setattr(delivery_module, "_spawn", lambda coro: coro.close())


@pytest.fixture(autouse=True)
def _capped(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", '{"twilio": 1600}')


def _install[C: FakeChannel](monkeypatch, channel: C) -> C:
    monkeypatch.setattr(notify_module, "resolve_channel", lambda name: channel)
    return channel


def _record(message_id: str, *, thread: str = _THREAD, route: str = "line", **update) -> ConversationRecord:
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": route,
        "door": "channel",
        "thread_id": thread,
        "client_address": _ADDRESS,
        "channel": "twilio",
        "our_identity": _IDENTITY,
        "origin": "client",
        "inbound_text": f"ask {message_id}",
        "answer_status": "answered",
        "answer": "the answer",
        "delivery_status": DeliveryStatus.DELIVERED,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(update)
    return ConversationRecord(**fields)


def _send(message_id: str, *, thread: str = _THREAD, route: str = "line", ask: AskSend | None = None):
    return _record(
        message_id,
        thread=thread,
        route=route,
        origin="send",
        sent_by="ask" if ask is not None else "notify",
        inbound_text="",
        delivery_status=DeliveryStatus.SENDING,
        ask=ask,
    )


async def _sent_record(
    lua_env: LuaEnv, message_id: str, ids: list[str], *, thread: str = _THREAD, route: str = "line", ask=None
) -> ConversationRecord:
    """A send record begun and completed through the store, as a send that went out writes it."""
    record = _send(message_id, thread=thread, route=route, ask=ask)
    route_row = _channel_route(route)
    await lua_env.store.begin_send_record(record, route=route_row, settle_deadline=time.time() + 60)
    done = record.model_copy(update={"sends": [SentMessage(part=0, chars=3, provider_message_ids=ids)]})
    assert (await lua_env.store.complete_send_record(done, route=route_row)).code == 1
    return done


async def _bridge_reply(lua_env: LuaEnv, message_id: str, ids: list[str], **update) -> None:
    await create_record(lua_env.store, _record(message_id, **update))
    assert await lua_env.store.index_outbound("twilio", _IDENTITY, _ADDRESS, ids, message_id) == 1


def _ask(interaction_id: str) -> AskSend:
    return AskSend(interaction_id=interaction_id, answer_format="text", sensitive=False, attempts=1)


def _reply(medium_id: str) -> AskReply:
    return AskReply(answer="yes", medium_message_id=medium_id, answered_at=datetime.now(UTC))


async def _assert_matches(lua_env: LuaEnv, message_id: str) -> None:
    names = await owned_keys(lua_env, message_id)
    assert len(names) == len(set(names))
    assert await keys_naming(lua_env, message_id) == set(names)


async def _seed_thread(lua_env: LuaEnv, *, thread: str = _THREAD, route: str = "line", tag: str = "") -> list[str]:
    """A thread holding a two-file inbound (two records on one ref), a bridge reply, a notify send and an
    answered ask send; returns their record ids."""
    first, second = f"in-a{tag}", f"in-b{tag}"
    await create_record(lua_env.store, _record(first, thread=thread, route=route, inbound_message_id=f"file{tag}"))
    await create_record(lua_env.store, _record(second, thread=thread, route=route, inbound_message_id=f"file{tag}"))
    await _bridge_reply(lua_env, f"reply{tag}", [f"r{tag}-1"], thread=thread, route=route)
    await _sent_record(lua_env, f"notify{tag}", [f"n{tag}-1"], thread=thread, route=route)
    await _sent_record(lua_env, f"ask{tag}", [f"q{tag}-1"], thread=thread, route=route, ask=_ask(f"i{tag}"))
    assert await lua_env.store.record_ask_reply(f"i{tag}", _reply(f"a{tag}-1")) == ("recorded", f"ask{tag}")
    return [first, second, f"reply{tag}", f"notify{tag}", f"ask{tag}"]


async def _assert_forgotten(lua_env: LuaEnv, ids: list[str], *, thread: str, routes: list[str]) -> None:
    settings = lua_env.store.settings
    for message_id in ids:
        assert await lua_env.redis.exists(settings.record_key(message_id)) == 0
        assert await keys_naming(lua_env, message_id) == set()
    for prefix in (settings.message_ref_key_prefix, settings.outbound_index_key_prefix, settings.ask_send_key_prefix):
        assert [key async for key in lua_env.redis.scan_iter(match=f"{prefix}*")] == []
    for route in routes:
        assert await lua_env.redis.exists(settings.thread_index_key(route, thread)) == 0
        assert await lua_env.redis.zscore(settings.route_threads_key(route), thread) is None


# -- owned_keys names exactly the keys -----------------------------------------------------------


async def test_owned_keys_name_exactly_the_keys_naming_the_record_after_each_writer(lua_env):
    await lua_env.manager.put_route(_channel_route())

    await create_record(lua_env.store, _record("in-1", inbound_message_id="file-1"))
    await _assert_matches(lua_env, "in-1")

    record = _send("ask-1", ask=_ask("i-1"))
    await lua_env.store.begin_send_record(record, route=_channel_route(), settle_deadline=time.time() + 60)
    await _assert_matches(lua_env, "ask-1")
    assert len(await owned_keys(lua_env, "ask-1")) == 2  # the in-flight set and the ask key

    done = record.model_copy(update={"sends": [SentMessage(part=0, chars=3, provider_message_ids=["q-1"])]})
    assert (await lua_env.store.complete_send_record(done, route=_channel_route())).code == 1
    await _assert_matches(lua_env, "ask-1")

    assert await lua_env.store.record_ask_reply("i-1", _reply("a-1")) == ("recorded", "ask-1")
    await _assert_matches(lua_env, "ask-1")

    await _bridge_reply(lua_env, "reply-1", ["r-1", "r-2"])
    await _assert_matches(lua_env, "reply-1")


# -- forgetting a thread ---------------------------------------------------------------------------


@pytest.mark.parametrize("door", ["drop_thread", "delete_conversation_thread"])
async def test_forgetting_a_thread_leaves_no_key_naming_its_records(door, lua_env, checkpoint_saver):
    await lua_env.manager.put_route(_channel_route())
    ids = await _seed_thread(lua_env)

    if door == "drop_thread":
        assert await lua_env.store.drop_thread("line", _THREAD) == len(ids)
    else:
        assert (await ops.delete_conversation_thread("line", _THREAD))["removed"] == len(ids)

    await _assert_forgotten(lua_env, ids, thread=_THREAD, routes=["line"])


async def _linked_person(lua_env: LuaEnv, *routes: str) -> Person:
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    )
    person = standing_person(_PERSON, *routes, target_name="echo")
    second = person.addresses[0].model_copy(update={"channel": "other", "our_identity": "bot", "address": "u-1"})
    linked = person.model_copy(update={"addresses": [*person.addresses, second]})
    await lua_env.seed_person(linked)
    return linked


async def test_erasing_a_linked_person_leaves_no_key_naming_its_records_on_either_route(lua_env, checkpoint_saver):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.manager.put_route(_channel_route("line-b", "+15550009999"))
    await _linked_person(lua_env, "line", "line-b")
    ids = [
        *await _seed_thread(lua_env, thread=_PERSON_THREAD, route="line"),
        *await _seed_thread(lua_env, thread=_PERSON_THREAD, route="line-b", tag="-b"),
    ]

    result = await ops.delete_conversation_person(_PERSON)

    assert result["erased"] is True
    await _assert_forgotten(lua_env, ids, thread=_PERSON_THREAD, routes=["line", "line-b"])


async def test_a_key_naming_another_record_survives_this_records_delete(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await _sent_record(lua_env, "ask-1", ["q-1"], ask=_ask("i-1"))
    settings = lua_env.store.settings
    # Another record now holds the outbound id and the ask key this row still names.
    await lua_env.redis.set(settings.outbound_index_key("twilio", "q-1"), "other")
    await lua_env.redis.set(settings.ask_send_key("i-1"), "other")

    record = await lua_env.store.get_record("ask-1")
    assert record is not None
    assert await lua_env.store.delete_record(record)

    assert await lua_env.redis.get(settings.outbound_index_key("twilio", "q-1")) == "other"
    assert await lua_env.redis.get(settings.ask_send_key("i-1")) == "other"


# -- a writer on a removed row -------------------------------------------------------------------


async def test_an_ask_reply_on_a_row_removed_after_its_lookup_writes_nothing(lua_env, monkeypatch):
    await lua_env.manager.put_route(_channel_route())
    await _sent_record(lua_env, "ask-1", ["q-1"], ask=_ask("i-1"))
    store = lua_env.store
    write = store._write_ask_reply

    async def _forgotten_first(record, reply):
        assert await store.drop_thread("line", _THREAD) == 1
        return await write(record, reply)

    monkeypatch.setattr(store, "_write_ask_reply", _forgotten_first)

    assert await store.record_ask_reply("i-1", _reply("a-1")) == ("gone", "ask-1")
    assert await lua_env.redis.exists(store.settings.record_key("ask-1")) == 0
    assert await store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "a-1") == []
    assert await lua_env.redis.exists(store.settings.message_ref_key("twilio", _IDENTITY, _ADDRESS, "a-1")) == 0


async def test_an_ask_reply_after_the_forget_misses_the_ask_key_and_writes_nothing(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await _sent_record(lua_env, "ask-1", ["q-1"], ask=_ask("i-1"))
    assert await lua_env.store.drop_thread("line", _THREAD) == 1

    assert await lua_env.store.record_ask_reply("i-1", _reply("a-1")) == ("unknown", None)
    assert await keys_naming(lua_env, "ask-1") == set()
    assert await lua_env.redis.exists(lua_env.store.settings.record_key("ask-1")) == 0


async def test_the_chunk_index_on_a_removed_row_writes_nothing(lua_env):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _record("reply-1"))
    assert await lua_env.store.drop_thread("line", _THREAD) == 1

    assert await lua_env.store.index_outbound("twilio", _IDENTITY, _ADDRESS, ["r-1"], "reply-1") == -1
    assert await keys_naming(lua_env, "reply-1") == set()
    assert await lua_env.redis.exists(lua_env.store.settings.record_key("reply-1")) == 0


async def test_a_reply_whose_thread_is_forgotten_mid_send_indexes_nothing_and_warns(lua_env, monkeypatch, caplog):
    await lua_env.manager.put_route(_channel_route())
    await create_record(lua_env.store, _record("reply-1", delivery_status=DeliveryStatus.PENDING_DELIVERY))

    async def _forget():
        await lua_env.store.drop_thread("line", _THREAD)

    monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(ChunkChannel("w1", watch=_forget)))
    assert await lua_env.store.claim_delivery("reply-1", time.time(), "t-1", 120) == 1
    record = await lua_env.store.get_record("reply-1")
    assert record is not None

    with caplog.at_level(logging.WARNING):
        await delivery_channel_module._deliver_channel(lua_env.store, record, "t-1")

    assert await keys_naming(lua_env, "reply-1") == set()
    assert await lua_env.redis.exists(lua_env.store.settings.record_key("reply-1")) == 0
    assert any("reply-1" in message and "forgotten" in message and "w1-1" in message for message in caplog.messages)


# -- a send still going out when its thread is forgotten ---------------------------------------------


async def _start(channel: GatedChannel, recipient: str = _ADDRESS) -> asyncio.Future:
    task = asyncio.ensure_future(notify_user("hello", channel="twilio", recipient=recipient))
    await channel.entered.wait()
    return task


async def _assert_marked(lua_env: LuaEnv, record: ConversationRecord, thread: str) -> None:
    """Right after the forget: the row stands in ``sending``, in no thread index, marked, in its in-flight set."""
    settings = lua_env.store.settings
    assert record.delivery_status is DeliveryStatus.SENDING
    assert thread in await lua_env.redis.hget(settings.record_key(record.message_id), "forgotten_threads")
    assert await lua_env.redis.exists(settings.thread_index_key(record.route_name, thread)) == 0
    assert await lua_env.redis.sismember(settings.route_sending_key(record.route_name), record.message_id)


async def _assert_gone(lua_env: LuaEnv, message_id: str, thread: str, route: str = "line") -> None:
    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.record_key(message_id)) == 0
    assert await keys_naming(lua_env, message_id) == set()
    assert await lua_env.redis.exists(settings.thread_index_key(route, thread)) == 0
    assert await lua_env.redis.zscore(settings.route_threads_key(route), thread) is None
    assert await lua_env.redis.zscore(settings.status_index_key(DeliveryStatus.SENDING.value), message_id) is None


def _warned_forgotten(caplog, message_id: str) -> bool:
    return any(message_id in message and "forgotten" in message for message in caplog.messages)


async def test_a_send_completing_into_a_thread_forgotten_meanwhile_is_forgotten(
    lua_env, monkeypatch, caplog, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    channel = _install(monkeypatch, GatedChannel())
    task = await _start(channel)

    await ops.delete_conversation_thread("line", _THREAD)
    [record] = await send_records(lua_env)
    await _assert_marked(lua_env, record, _THREAD)
    with caplog.at_level(logging.WARNING):
        channel.release.set()
        assert await task == ["out-1"]

    await _assert_gone(lua_env, record.message_id, _THREAD)
    assert _warned_forgotten(caplog, record.message_id)


async def test_a_send_raising_after_its_thread_was_forgotten_is_forgotten_and_raises(lua_env, monkeypatch, caplog):
    await lua_env.manager.put_route(_channel_route())
    channel = _install(monkeypatch, RaisingGatedChannel())
    task = await _start(channel)

    await lua_env.store.drop_thread("line", _THREAD)
    [record] = await send_records(lua_env)
    await _assert_marked(lua_env, record, _THREAD)
    with caplog.at_level(logging.WARNING):
        channel.release.set()
        with pytest.raises(ChannelDeliveryError, match="the medium refused the send"):
            await task

    await _assert_gone(lua_env, record.message_id, _THREAD)
    assert _warned_forgotten(caplog, record.message_id)


@pytest.mark.parametrize("ends", ["raise", "succeed"])
async def test_a_send_in_a_linked_person_thread_erased_meanwhile(ends, lua_env, monkeypatch, caplog, checkpoint_saver):
    await lua_env.manager.put_route(_channel_route())
    await _linked_person(lua_env, "line")
    channel = _install(monkeypatch, RaisingGatedChannel() if ends == "raise" else GatedChannel())
    task = await _start(channel, "+" + _PERSON)
    [record] = await send_records(lua_env)
    assert record.thread_id == _PERSON_THREAD

    assert (await ops.delete_conversation_person(_PERSON))["erased"] is True
    await _assert_marked(lua_env, (await send_records(lua_env))[0], _PERSON_THREAD)
    with caplog.at_level(logging.WARNING):
        channel.release.set()
        if ends == "raise":
            with pytest.raises(ChannelDeliveryError):
                await task
        else:
            assert await task == ["out-1"]

    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.thread_index_key("line", _PERSON_THREAD)) == 0
    assert await lua_env.redis.zscore(settings.route_threads_key("line"), _PERSON_THREAD) is None
    if ends == "raise":
        await _assert_gone(lua_env, record.message_id, _PERSON_THREAD)
        assert _warned_forgotten(caplog, record.message_id)
    else:
        [landed] = await send_records(lua_env)
        own_thread = route_thread_id("line", "+" + _PERSON)
        assert landed.thread_id == own_thread
        assert landed.delivery_status is DeliveryStatus.PROVISIONAL
        assert await lua_env.redis.zrange(settings.thread_index_key("line", own_thread), 0, -1) == [landed.message_id]
    with pytest.raises(NotFoundError):
        await ops.delete_conversation_thread("line", _PERSON_THREAD)


async def test_a_crashed_send_in_a_forgotten_thread_is_forgotten_by_the_settle(lua_env, monkeypatch, caplog):
    await lua_env.manager.put_route(_channel_route())

    async def _stopped(*args, **kwargs):
        return None

    monkeypatch.setattr(sends_module, "_complete", _stopped)
    _install(monkeypatch, FakeChannel())
    await notify_user("hello", channel="twilio", recipient=_ADDRESS)
    [record] = await send_records(lua_env)

    await lua_env.store.drop_thread("line", _THREAD)
    [marked] = await send_records(lua_env)
    await _assert_marked(lua_env, marked, _THREAD)
    await past_deadline(lua_env, record.message_id)
    with caplog.at_level(logging.WARNING):
        await delivery_module.sweep_stalled_deliveries()

    await _assert_gone(lua_env, record.message_id, _THREAD)
    assert _warned_forgotten(caplog, record.message_id)


class _MovingChannel(GatedChannel):
    """A gated channel addressed by a handle before the send and by a numeric id in its outcome."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    async def notify(self, notification):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ChannelDeliveryError("the medium refused the send", retryable=False)
        if notification.recipient == "@handle":
            return SendOutcome(our_identity=_IDENTITY, address="12345", message_ids=["m-9"])
        return self.outcome(notification, ["m-other"])


async def test_a_send_moving_into_a_forgotten_thread_is_forgotten_and_its_neighbour_lands(lua_env, monkeypatch, caplog):
    await lua_env.manager.put_route(_channel_route())
    channel = _install(monkeypatch, _MovingChannel())
    moving = await _start(channel, "@handle")
    neighbour = asyncio.ensure_future(notify_user("hello", channel="twilio", recipient=_ADDRESS))
    while len(await send_records(lua_env)) < 2:
        await asyncio.sleep(0)
    by_address = {record.client_address: record for record in await send_records(lua_env)}

    landing = route_thread_id("line", "12345")
    await lua_env.store.drop_thread("line", landing)
    for record in by_address.values():
        assert landing in await lua_env.redis.hget(
            lua_env.store.settings.record_key(record.message_id), "forgotten_threads"
        )
    with caplog.at_level(logging.WARNING):
        channel.release.set()
        assert await moving == ["m-9"]
        assert await neighbour == ["m-other"]

    await _assert_gone(lua_env, by_address["@handle"].message_id, landing)
    assert (
        await lua_env.redis.exists(lua_env.store.settings.thread_index_key("line", route_thread_id("line", "@handle")))
        == 0
    )
    [landed] = await send_records(lua_env)
    assert landed.message_id == by_address[_ADDRESS].message_id
    assert landed.thread_id == _THREAD
    assert landed.delivery_status is DeliveryStatus.PROVISIONAL
    assert _warned_forgotten(caplog, by_address["@handle"].message_id)


@pytest.mark.parametrize("ends", ["succeed", "raise"])
async def test_a_send_moving_out_of_a_forgotten_thread_is_recorded_where_it_lands(ends, lua_env, monkeypatch, caplog):
    await lua_env.manager.put_route(_channel_route())
    begin_thread = route_thread_id("line", "@handle")
    await create_record(
        lua_env.store,
        _record("earlier", thread=begin_thread, client_address="@handle", delivery_status=DeliveryStatus.FAILED),
    )
    channel = _install(monkeypatch, _MovingChannel(fail=ends == "raise"))
    task = await _start(channel, "@handle")
    [record] = await send_records(lua_env)

    await lua_env.store.drop_thread("line", begin_thread)
    with caplog.at_level(logging.WARNING):
        channel.release.set()
        if ends == "raise":
            with pytest.raises(ChannelDeliveryError):
                await task
        else:
            assert await task == ["m-9"]

    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.thread_index_key("line", begin_thread)) == 0
    assert await lua_env.redis.exists(settings.record_key("earlier")) == 0
    if ends == "raise":
        await _assert_gone(lua_env, record.message_id, begin_thread)
        return
    landing = route_thread_id("line", "12345")
    [landed] = await send_records(lua_env)
    assert landed.thread_id == landing
    assert landed.delivery_status is DeliveryStatus.PROVISIONAL
    assert await lua_env.redis.zrange(settings.thread_index_key("line", landing), 0, -1) == [landed.message_id]
    assert await lua_env.redis.zscore(settings.route_threads_key("line"), landing) is not None
    assert await lua_env.redis.get(settings.outbound_index_key("twilio", "m-9")) == landed.message_id
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, "12345", "m-9") == [landed.message_id]
    assert not _warned_forgotten(caplog, landed.message_id)
