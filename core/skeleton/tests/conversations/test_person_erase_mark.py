"""The erase mark of a person: who reads it, what an erase refused or interrupted leaves, and the re-read under it.

The erase is stopped at a chosen step by a pause on one store call. The stores run the real
script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.conversations import PersonAddress, TargetConversationConfig

from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.persons import PairingTarget, PersonErasingError
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module
from tai42_skeleton.operations.errors import ConflictError, NotFoundError

from .conftest import FakeChannel, LuaEnv, _channel_route, create_record, pause_call, send_records, standing_person

_PERSON = "15550002222"
_ADDRESS = f"+{_PERSON}"
_IDENTITY = "+15550001111"
_PERSON_THREAD = person_thread_id(_PERSON)
_TARGET = PairingTarget(target_kind="agent", target_name="echo")


@pytest.fixture(autouse=True)
def _no_grace_confirms(monkeypatch):
    monkeypatch.setattr(delivery_module, "_spawn", lambda coro: coro.close())


@pytest.fixture(autouse=True)
def _capped(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", '{"twilio": 1600}')


async def _standing(lua_env: LuaEnv, person_id: str = _PERSON, **update):
    """A linked person of the multichannel agent ``echo``: its twilio address on ``line`` and an api address."""
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    )
    person = standing_person(person_id, "line", target_name="echo")
    second = person.addresses[0].model_copy(
        update={"door": "api", "channel": None, "our_identity": None, "address": "alice/u-7", "routes": ["chat"]}
    )
    person = person.model_copy(update={"addresses": [*person.addresses, second], **update})
    await lua_env.seed_person(person)
    return person


async def _lookup(lua_env: LuaEnv, address: str = _ADDRESS):
    return await lua_env.manager.persons.get_person(
        _TARGET, door="channel", channel="twilio", our_identity=_IDENTITY, address=address
    )


async def _index_names(lua_env: LuaEnv, person_id: str) -> list[str]:
    index = await lua_env.redis.hgetall(lua_env.store.settings.keyspace.person_index_key("agent", "echo"))
    return sorted(field for field, owner in index.items() if owner == person_id)


async def test_a_marked_person_reads_as_absent_until_its_mark_is_removed(lua_env):
    person = await _standing(lua_env)
    persons = lua_env.manager.persons

    assert await persons.mark_erasing(_PERSON) is True

    assert await _lookup(lua_env) is None
    assert await persons.get_by_id(_PERSON) is None
    assert await persons.get_by_id(_PERSON, include_erasing=True) == person
    await persons.clear_erasing(_PERSON)
    assert await _lookup(lua_env) == person
    assert await persons.get_by_id(_PERSON) == person


async def test_a_mark_asked_for_a_gone_person_sets_nothing(lua_env):
    assert await lua_env.manager.persons.mark_erasing(_PERSON) is False
    assert await lua_env.redis.exists(lua_env.store.settings.keyspace.person_erasing_key(_PERSON)) == 0


async def test_the_row_delete_removes_the_mark_with_the_row(lua_env):
    person = await _standing(lua_env)
    persons = lua_env.manager.persons
    assert await persons.mark_erasing(_PERSON) is True
    read = await persons.read_for_erase(_PERSON)

    erased, _fields = await persons.erase(person, read.absorbed_ids, read.route_threads)

    settings = lua_env.store.settings
    assert erased is True
    assert await lua_env.redis.exists(settings.keyspace.person_key(_PERSON)) == 0
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 0


async def test_an_inbound_written_before_the_mark_refuses_the_erase_and_gives_the_person_back(
    lua_env, monkeypatch, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    person = await _standing(lua_env)
    fields = await _index_names(lua_env, _PERSON)

    async def _schedule(caps, *, intake, **kwargs):
        caps.release_thread_slot(intake.thread_id)

    monkeypatch.setattr(intake_module, "_schedule_turn", _schedule)
    marking = pause_call(monkeypatch, lua_env.manager.persons, "mark_erasing")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await marking.reached.wait()
    accepted = await intake_module.accept(
        "twilio", _IDENTITY, _ADDRESS, _ADDRESS, "hi", "PID1", medium_message_id=None, reply_to_medium_message_id=None
    )
    marking.go.set()

    with pytest.raises(ConflictError, match="has a turn in flight"):
        await erase

    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 0
    assert await lua_env.manager.persons.get_by_id(_PERSON) == person
    assert await _index_names(lua_env, _PERSON) == fields
    stored = await lua_env.store.get_record(accepted)
    assert stored is not None
    assert stored.thread_id == _PERSON_THREAD


async def test_an_interrupted_erase_keeps_the_mark_and_a_retry_finishes_it(lua_env, monkeypatch, checkpoint_saver):
    await lua_env.manager.put_route(_channel_route())
    await _standing(lua_env)
    monkeypatch.setattr(notify_module, "resolve_channel", lambda name: FakeChannel())
    real_state_erase = threads_delete_module._erase_person_state
    calls: list[str] = []

    async def _failing_once(*args, **kwargs):
        calls.append("state")
        if len(calls) == 1:
            raise RuntimeError("the states store is unreachable")
        return await real_state_erase(*args, **kwargs)

    monkeypatch.setattr(threads_delete_module, "_erase_person_state", _failing_once)

    with pytest.raises(RuntimeError, match="the states store is unreachable"):
        await ops.delete_conversation_person(_PERSON)

    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.keyspace.person_key(_PERSON)) == 1
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 1
    await notify_user("hello", channel="twilio", recipient=_ADDRESS)
    [sent] = await send_records(lua_env)
    assert sent.thread_id == route_thread_id("line", _ADDRESS)
    with pytest.raises(NotFoundError):
        await ops.get_conversation_person(_PERSON)
    with pytest.raises(PersonErasingError) as refused:
        await ops.set_conversation_person_locale(_PERSON, "he-IL")
    assert refused.value.status == 503
    assert str(refused.value) == str(PersonErasingError(_PERSON, "the locale change"))

    assert (await ops.delete_conversation_person(_PERSON))["erased"] is True

    assert await lua_env.redis.exists(settings.keyspace.person_key(_PERSON)) == 0
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 0
    assert await _index_names(lua_env, _PERSON) == []


async def test_a_merge_landing_before_the_mark_is_erased_on_every_route_the_merged_person_brought(
    lua_env, monkeypatch, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    other_route = _channel_route("line-b", "+15550009999")
    await lua_env.manager.put_route(other_route)
    earlier = datetime.now(UTC) - timedelta(days=1)
    await _standing(lua_env, created_at=earlier)
    other = standing_person("15550003333", "line-b", target_name="echo").model_copy(
        update={
            "addresses": [
                PersonAddress(
                    door="channel",
                    routes=["line-b"],
                    channel="twilio",
                    our_identity="+15550009999",
                    address="+15550003333",
                    linked_at=datetime.now(UTC),
                )
            ]
        }
    )
    await lua_env.seed_person(other)
    marking = pause_call(monkeypatch, lua_env.manager.persons, "mark_erasing")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await marking.reached.wait()

    survivor = await lua_env.manager.persons.merge(other.person_id, _PERSON)
    assert survivor.person_id == _PERSON
    now = time.time()
    await create_record(
        lua_env.store,
        ConversationRecord(
            message_id="after-link-1",
            route_name="line-b",
            door="channel",
            thread_id=_PERSON_THREAD,
            client_address="+15550003333",
            channel="twilio",
            our_identity="+15550009999",
            origin="client",
            inbound_text="hi",
            answer_status="answered",
            answer="ok",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        ),
        route=other_route,
    )
    marking.go.set()

    assert (await erase)["erased"] is True

    settings = lua_env.store.settings
    for route in ("line", "line-b"):
        assert await lua_env.redis.exists(settings.keyspace.thread_index_key(route, _PERSON_THREAD)) == 0
        assert await lua_env.redis.zscore(settings.keyspace.route_threads_key(route), _PERSON_THREAD) is None
    assert await lua_env.redis.exists(settings.keyspace.record_key("after-link-1")) == 0


async def _person_rows(lua_env: LuaEnv) -> set[str]:
    prefix = f"{lua_env.store.settings.prefix}:person:"
    ids = (key.removeprefix(prefix) for key in await lua_env.redis.keys(f"{prefix}*"))
    return {person_id for person_id in ids if ":" not in person_id}


@pytest.mark.parametrize("marked_first", [True, False])
@pytest.mark.parametrize("marked_is_earlier", [True, False])
async def test_a_merge_with_a_marked_person_is_refused_and_moves_nothing(
    marked_is_earlier, marked_first, lua_env, checkpoint_saver
):
    await lua_env.manager.put_route(_channel_route())
    now = datetime.now(UTC)
    person = await _standing(lua_env, created_at=now - timedelta(days=1) if marked_is_earlier else now)
    other = standing_person("15550003333", "line", target_name="echo").model_copy(
        update={"created_at": now if marked_is_earlier else now - timedelta(days=1)}
    )
    await lua_env.seed_person(other)
    other_fields = await _index_names(lua_env, other.person_id)
    persons = lua_env.manager.persons
    assert await persons.mark_erasing(_PERSON) is True

    with pytest.raises(PersonErasingError):
        await persons.merge(*((_PERSON, other.person_id) if marked_first else (other.person_id, _PERSON)))

    assert await persons.get_by_id(_PERSON, include_erasing=True) == person
    assert await persons.get_by_id(other.person_id) == other
    assert await _index_names(lua_env, other.person_id) == other_fields
    await persons.clear_erasing(_PERSON)
    assert (await ops.delete_conversation_person(_PERSON))["erased"] is True
    assert await persons.get_by_id(other.person_id) == other
    assert await _index_names(lua_env, other.person_id) == other_fields


async def test_a_detach_from_a_marked_person_is_refused_and_writes_no_fresh_person(lua_env):
    person = await _standing(lua_env)
    fields = await _index_names(lua_env, _PERSON)
    persons = lua_env.manager.persons
    assert await persons.mark_erasing(_PERSON) is True

    with pytest.raises(PersonErasingError):
        await persons.detach(_PERSON, door="channel", channel="twilio", our_identity=_IDENTITY, address=_ADDRESS)

    assert await _person_rows(lua_env) == {_PERSON}
    assert await _index_names(lua_env, _PERSON) == fields
    assert await persons.get_by_id(_PERSON, include_erasing=True) == person


async def test_a_locale_set_on_a_marked_person_is_refused_and_changes_nothing(lua_env):
    person = await _standing(lua_env, locale="en-US")
    persons = lua_env.manager.persons
    assert await persons.mark_erasing(_PERSON) is True

    with pytest.raises(PersonErasingError):
        await persons.set_locale(_PERSON, "he-IL")

    assert await persons.get_by_id(_PERSON, include_erasing=True) == person


async def test_a_first_contact_while_another_person_is_marked_creates_a_fresh_person(lua_env):
    await _standing(lua_env)
    persons = lua_env.manager.persons
    assert await persons.mark_erasing(_PERSON) is True
    row = PersonAddress(
        door="channel",
        routes=["line"],
        channel="twilio",
        our_identity=_IDENTITY,
        address="+15550004444",
        linked_at=datetime.now(UTC),
    )

    fresh, created = await persons.ensure_provisional(_TARGET, row)

    assert created is True
    assert fresh.person_id != _PERSON
    assert await _person_rows(lua_env) == {_PERSON, fresh.person_id}


async def test_a_contact_from_a_marked_persons_address_is_refused(lua_env):
    person = await _standing(lua_env)
    persons = lua_env.manager.persons
    assert await persons.mark_erasing(_PERSON) is True

    with pytest.raises(PersonErasingError):
        await persons.ensure_provisional(_TARGET, person.addresses[0])

    assert await _person_rows(lua_env) == {_PERSON}
