"""A person being erased takes no new record into its thread, from a send or from an inbound.

The erase is stopped at a chosen step by a pause on one store call while a send or an inbound
runs against it. Each case ends with a key scan: no thread index and no ``route_threads`` member
for the person thread on any route of the person's, no person row, no erase mark, no index
mapping, and a thread delete of the person thread is a 404. A send or an inbound that lands in a
route chat the erase never visited stops the erase's final step, and the retried erase wipes it.
The stores run the real script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.conversations import (
    ConversationEvent,
    ConversationEventSubmission,
    Person,
    TargetConversationConfig,
)

from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus, PersonThreadClosedError
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.conversations.turn import api_door as api_door_module
from tai42_skeleton.conversations.turn import event_door as event_door_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import ConflictError, NotFoundError

from .conftest import (
    FakeChannel,
    GatedChannel,
    LuaEnv,
    RaisingGatedChannel,
    _api_route,
    _channel_route,
    _connected,
    _tool_channel_route,
    create_record,
    keys_naming,
    past_deadline,
    pause_call,
    send_records,
    standing_person,
)

_PERSON = "15550002222"
_ADDRESS = f"+{_PERSON}"
_PERSON_THREAD = person_thread_id(_PERSON)
_OWN_THREAD = route_thread_id("line", _ADDRESS)
_API_ADDRESS = "alice/u-7"


@pytest.fixture(autouse=True)
def _no_grace_confirms(monkeypatch):
    monkeypatch.setattr(delivery_module, "_spawn", lambda coro: coro.close())


@pytest.fixture(autouse=True)
def _capped(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", '{"twilio": 1600}')


@pytest.fixture
def scheduled(monkeypatch) -> list[ConversationRecord]:
    """The intakes a door scheduled a turn for; no turn runs."""
    seen: list[ConversationRecord] = []

    async def _schedule(caps, *, intake, **kwargs):
        seen.append(intake)
        caps.release_thread_slot(intake.thread_id)

    for module in (intake_module, api_door_module, event_door_module):
        monkeypatch.setattr(module, "_schedule_turn", _schedule)
    return seen


def _install[C: FakeChannel](monkeypatch, channel: C) -> C:
    monkeypatch.setattr(notify_module, "resolve_channel", lambda name: channel)
    return channel


async def _linked_person(
    lua_env: LuaEnv,
    *routes: str,
    target_kind: ConversationTargetKind = "agent",
    target_name: str = "echo",
    api_route: str = "chat",
) -> Person:
    """A person of a multichannel target with its twilio address on ``routes`` and an api address on ``api_route``."""
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind=target_kind, target_name=target_name, multichannel=True)
    )
    person = standing_person(_PERSON, *routes, target_name=target_name).model_copy(update={"target_kind": target_kind})
    second = person.addresses[0].model_copy(
        update={"door": "api", "channel": None, "our_identity": None, "address": _API_ADDRESS, "routes": [api_route]}
    )
    linked = person.model_copy(update={"addresses": [*person.addresses, second]})
    await lua_env.seed_person(linked)
    return linked


async def _assert_erased(lua_env: LuaEnv, person: Person, *routes: str) -> None:
    settings = lua_env.store.settings
    for route in routes:
        assert await lua_env.redis.exists(settings.keyspace.thread_index_key(route, _PERSON_THREAD)) == 0
        assert await lua_env.redis.zscore(settings.keyspace.route_threads_key(route), _PERSON_THREAD) is None
    assert await lua_env.redis.exists(settings.keyspace.person_key(_PERSON)) == 0
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 0
    index = await lua_env.redis.hgetall(settings.keyspace.person_index_key(person.target_kind, person.target_name))
    assert _PERSON not in index.values()
    with pytest.raises(NotFoundError):
        await ops.delete_conversation_thread(routes[0], _PERSON_THREAD)


def _drops_person_thread_on(route_name: str):
    return lambda route, thread_id: route == route_name and thread_id == _PERSON_THREAD


async def _erase_paused_before_its_row_delete(lua_env: LuaEnv, monkeypatch):
    """Start the erase, held after its drop of the person thread on ``line`` and before its row delete."""
    walked = pause_call(monkeypatch, lua_env.store, "drop_thread", after=True, when=_drops_person_thread_on("line"))
    row = pause_call(monkeypatch, lua_env.manager.persons, "erase")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    return erase, walked, row


def _unvisited(thread: str) -> str:
    """The final step's refusal of a thread the person first wrote in after the erase's last read."""
    return re.escape(f"a write by {_PERSON!r} in the thread {thread!r} landed while the erase of {_PERSON!r} ran")


@pytest.mark.parametrize("ends", ["raise", "succeed"])
async def test_a_send_begun_after_the_walk_lands_nowhere_in_the_erased_person_thread(
    ends, lua_env, monkeypatch, caplog, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    person = await _linked_person(lua_env, "line")
    channel = _install(monkeypatch, RaisingGatedChannel() if ends == "raise" else GatedChannel())
    begin = pause_call(monkeypatch, lua_env.store, "begin_send_record")
    send = asyncio.ensure_future(notify_user("hello", channel="twilio", recipient=_ADDRESS))
    await begin.reached.wait()

    erase, walked, row = await _erase_paused_before_its_row_delete(lua_env, monkeypatch)
    await walked.reached.wait()
    begin.go.set()
    await channel.entered.wait()
    [begun] = await send_records(lua_env)
    assert begun.thread_id == _PERSON_THREAD
    assert await lua_env.redis.hget(
        lua_env.store.settings.keyspace.record_key(begun.message_id), "forgotten_threads"
    ) in (
        None,
        "",
    )
    walked.go.set()
    await row.reached.wait()

    with caplog.at_level(logging.WARNING):
        channel.release.set()
        if ends == "raise":
            with pytest.raises(ChannelDeliveryError, match="the medium refused the send"):
                await send
        else:
            assert await send == ["out-1"]
    row.go.set()

    settings = lua_env.store.settings
    if ends == "raise":
        assert (await erase)["erased"] is True
        assert await lua_env.redis.exists(settings.keyspace.record_key(begun.message_id)) == 0
        assert await keys_naming(lua_env, begun.message_id) == set()
        assert any(begun.message_id in message and _PERSON_THREAD in message for message in caplog.messages)
        await _assert_erased(lua_env, person, "line")
        return
    with pytest.raises(ConflictError, match=_unvisited(_OWN_THREAD)):
        await erase
    [landed] = await send_records(lua_env)
    assert landed.thread_id == _OWN_THREAD
    assert landed.delivery_status is DeliveryStatus.PROVISIONAL
    assert await lua_env.redis.zrange(settings.keyspace.thread_index_key("line", _OWN_THREAD), 0, -1) == [
        landed.message_id
    ]
    assert await lua_env.store.resolve_outbound("twilio", "out-1") == landed.message_id
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 1

    assert (await ops.delete_conversation_person(_PERSON))["erased"] is True

    assert await send_records(lua_env) == []
    assert await lua_env.store.resolve_outbound("twilio", "out-1") is None
    assert await lua_env.redis.exists(settings.keyspace.thread_index_key("line", _OWN_THREAD)) == 0
    await _assert_erased(lua_env, person, "line")


async def test_a_send_begun_after_the_mark_into_the_persons_route_thread_is_wiped_with_it(
    lua_env, monkeypatch, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    person = await _linked_person(lua_env, "line")
    _install(monkeypatch, FakeChannel())
    marked = pause_call(monkeypatch, lua_env.manager.persons, "mark_erasing", after=True)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await marked.reached.wait()

    await notify_user("hello", channel="twilio", recipient=_ADDRESS)

    [record] = await send_records(lua_env)
    assert record.thread_id == _OWN_THREAD
    assert await lua_env.redis.hget(lua_env.store.settings.keyspace.record_key(record.message_id), "writer") == _PERSON
    marked.go.set()
    result = await erase
    assert (result["erased"], result["removed"]) == (True, 1)
    assert await send_records(lua_env) == []
    assert await lua_env.redis.exists(lua_env.store.settings.keyspace.thread_index_key("line", _OWN_THREAD)) == 0
    await _assert_erased(lua_env, person, "line")


async def test_a_stranded_send_begun_in_the_person_thread_after_the_walk_is_deleted_by_the_settle(
    lua_env, monkeypatch, caplog, checkpoint_saver
):
    route = _channel_route()
    await lua_env.manager.put_route(route)
    person = await _linked_person(lua_env, "line")
    erase, walked, row = await _erase_paused_before_its_row_delete(lua_env, monkeypatch)
    await walked.reached.wait()
    now = time.time()
    stranded = ConversationRecord(
        message_id="stranded-1",
        route_name="line",
        door="channel",
        thread_id=_PERSON_THREAD,
        client_address=_ADDRESS,
        channel="twilio",
        our_identity="+15550001111",
        origin="send",
        sent_by="notify",
        inbound_text="",
        answer_status="answered",
        answer="hello",
        delivery_status=DeliveryStatus.SENDING,
        created_at=now,
        updated_at=now,
    )
    await lua_env.store.begin_send_record(stranded, route=route, settle_deadline=now + 60)
    walked.go.set()
    row.go.set()
    assert (await erase)["erased"] is True

    await past_deadline(lua_env, "stranded-1")
    with caplog.at_level(logging.WARNING):
        await delivery_module.sweep_stalled_deliveries()

    assert await lua_env.redis.exists(lua_env.store.settings.keyspace.record_key("stranded-1")) == 0
    assert await keys_naming(lua_env, "stranded-1") == set()
    assert any("stranded-1" in message and _PERSON_THREAD in message for message in caplog.messages)
    await _assert_erased(lua_env, person, "line")


async def _record_ids(lua_env: LuaEnv) -> set[str]:
    prefix = lua_env.store.settings.keyspace.record_key("")
    return {key.removeprefix(prefix) for key in await lua_env.redis.keys(f"{prefix}*")}


async def _refused_while_the_erase_walks(lua_env: LuaEnv, monkeypatch, door, route_name: str):
    """Run ``door()`` with its create held until the erase has dropped the person thread on ``route_name``.

    Answers the erase task and its pause after the drop, still held; the door's error is raised.
    """
    held = pause_call(monkeypatch, lua_env.store, "create_record")
    accepted = asyncio.ensure_future(door())
    await held.reached.wait()
    walked = pause_call(monkeypatch, lua_env.store, "drop_thread", after=True, when=_drops_person_thread_on(route_name))
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await walked.reached.wait()
    before = await _record_ids(lua_env)
    held.go.set()
    with pytest.raises(PersonThreadClosedError, match=_PERSON_THREAD):
        await accepted
    assert await _record_ids(lua_env) == before
    # The erase's own reservation is the one slot left on the person thread.
    assert caps_module.get_turn_caps()._thread_waiters.get(_PERSON_THREAD) == 1
    return erase, walked


async def test_a_channel_inbound_created_after_the_walk_is_refused_and_a_resend_goes_with_the_retried_erase(
    lua_env, monkeypatch, scheduled, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    person = await _linked_person(lua_env, "line")

    def _inbound(provider_message_id: str):
        return intake_module.accept(
            "twilio",
            "+15550001111",
            _ADDRESS,
            _ADDRESS,
            "hi",
            provider_message_id,
            medium_message_id="medium-1",
            reply_to_medium_message_id=None,
        )

    erase, walked = await _refused_while_the_erase_walks(lua_env, monkeypatch, lambda: _inbound("PID1"), "line")
    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.keyspace.dedupe_key("twilio", "PID1")) == 0
    assert await lua_env.store.resolve_message_ref("twilio", "+15550001111", _ADDRESS, "medium-1") == []
    assert scheduled == []

    resent = await _inbound("PID1")

    [intake] = scheduled
    assert intake.message_id == resent
    assert intake.thread_id == _OWN_THREAD
    walked.go.set()
    with pytest.raises(ConflictError, match=_unvisited(_OWN_THREAD)):
        await erase
    assert (await lua_env.store.get_record(resent)).thread_id == _OWN_THREAD
    answered = intake.model_copy(
        update={"answer_status": "answered", "answer": "ok", "delivery_status": DeliveryStatus.PENDING_DELIVERY}
    )
    assert await lua_env.store.complete_turn(answered) == 1

    assert (await ops.delete_conversation_person(_PERSON))["erased"] is True

    assert await lua_env.store.get_record(resent) is None
    await _assert_erased(lua_env, person, "line", "chat")


async def test_an_api_message_created_after_the_walk_is_refused(lua_env, monkeypatch, scheduled, checkpoint_saver):
    await lua_env.manager.put_route(_channel_route())
    await lua_env.manager.put_route(_api_route())
    person = await _linked_person(lua_env, "line")

    def _message():
        return api_door_module.submit_api_message(
            "chat", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected
        )

    erase, walked = await _refused_while_the_erase_walks(lua_env, monkeypatch, _message, "chat")
    assert scheduled == []
    walked.go.set()
    assert (await erase)["erased"] is True
    await _assert_erased(lua_env, person, "line", "chat")


async def test_an_event_by_thread_id_created_after_the_walk_is_refused(
    lua_env, monkeypatch, scheduled, checkpoint_saver
):
    route = _tool_channel_route()
    await lua_env.manager.put_route(route)
    person = await _linked_person(lua_env, "tool-line", target_kind="tool", target_name="echo-tool")
    now = time.time()
    await create_record(
        lua_env.store,
        ConversationRecord(
            message_id="earlier-1",
            route_name="tool-line",
            door="channel",
            thread_id=_PERSON_THREAD,
            client_address=_ADDRESS,
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="hi",
            answer_status="answered",
            answer="ok",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        ),
        route=route,
    )
    submission = ConversationEventSubmission(
        thread_id=_PERSON_THREAD, event=ConversationEvent(event_id="evt-1", kind="k", payload={})
    )

    def _event():
        return event_door_module.submit_event("tool-line", submission, "alice", client_connected=_connected)

    erase, walked = await _refused_while_the_erase_walks(lua_env, monkeypatch, _event, "tool-line")
    assert await lua_env.redis.exists(lua_env.store.settings.keyspace.event_dedupe_key("tool-line", "evt-1")) == 0
    assert scheduled == []
    walked.go.set()
    assert (await erase)["erased"] is True
    await _assert_erased(lua_env, person, "tool-line")
