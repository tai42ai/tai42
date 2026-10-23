"""A person's erase clears every person it absorbed by a link: their threads, parks, checkpoints and state.

Each case ends with a scan of every store for the absorbed person's ids, threads and messages.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from tai42_contract.interactions import PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import completion_delivery as completion_delivery_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.conversations.turn import redrive as redrive_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module
from tai42_skeleton.runs.chokepoint import delivery_fire

from .conftest import _channel_route, past_deadline, pause_call
from .linked_erase import (
    _TARGET_KEY,
    CHAT,
    CHAT_2,
    LINE,
    LINE_B,
    LINE_B_IDENTITY,
    LINE_IDENTITY,
    STATE,
    TARGET,
    HeldSend,
    World,
    api_address,
    assert_clean,
    channel_address,
    leftovers,
    person,
    person_thread_id,
    route_thread_id,
)
from .route_thread_writes import writers
from .test_person_erase_sends import scheduled  # noqa: F401  # the doors' turn scheduling, held
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture

# The package re-exports the ``operator_send`` function under the module's own name.
operator_send_module = importlib.import_module("tai42_skeleton.conversations.turn.operator_send")

_P, _Q = "p0000001", "q0000001"
_P1, _P2 = "+15550001001", "+15550001002"
_Q1, _Q2 = "+15550002001", "+15550002002"


@pytest.fixture
async def world(lua_env, states_pg, interactions_parks, checkpoint_saver, monkeypatch) -> World:  # noqa: F811
    world = World(lua_env=lua_env, states_pg=states_pg, parks=interactions_parks, saver=checkpoint_saver)
    world.install_channel(monkeypatch)
    return world


async def _writers(world: World, thread: str) -> set[str]:
    """The writers of the records standing in ``thread``."""
    return await writers(world.lua_env, thread)


async def _threads_of(world: World, writer: str) -> set[str]:
    """The threads on ``writer``'s visit list."""
    return await world.redis.smembers(world.settings.writer_threads_key(writer))


async def _two_address_person(world: World, person_id: str, first: str, second: str, *, age_days: int) -> list[str]:
    """A person with an address on ``line`` and one on ``line-b``, its conversation in its person thread.

    An inbound on each route, a ``notify_user`` send and an ``ask``, a park on its thread and one on
    its ``person`` subject, and a ``thread``-subject state. Answers the message ids.
    """
    await world.seed(
        person(person_id, channel_address(LINE, first), channel_address(LINE_B, second), age_days=age_days)
    )
    thread = person_thread_id(person_id)
    ids = [
        await world.record(f"{person_id}-in-1", LINE, thread, first),
        await world.record(f"{person_id}-in-2", LINE_B, thread, second),
        await world.notify(first),
        await world.ask(second, f"{person_id}-ask", our_identity=LINE_B_IDENTITY),
    ]
    await world.park(f"{person_id}-park-thread", thread)
    await world.park(f"{person_id}-park-person", thread, person_id=person_id)
    world.thread_state(thread)
    return ids


async def test_an_erase_clears_the_person_it_absorbed_by_a_link(world):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    for message_id in [*p_ids, *q_ids]:
        assert (await world.lua_env.store.get_record(message_id)).thread_id in (
            person_thread_id(_P),
            person_thread_id(_Q),
        )
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result == {
        "person_id": _P,
        "removed": len(p_ids) + len(q_ids),
        "erased": True,
        "states_erased": 4,
        "absorbed_person_ids": [_Q],
    }
    await assert_clean(
        world,
        _P,
        _Q,
        _P1,
        _P2,
        _Q1,
        _Q2,
        *p_ids,
        *q_ids,
        threads=(person_thread_id(_P), person_thread_id(_Q)),
        parks=(f"{_P}-park-thread", f"{_P}-park-person", f"{_Q}-park-thread", f"{_Q}-park-person"),
    )


async def _single_address_person(world: World, person_id: str, address: str) -> tuple[str, list[str]]:
    """A person with one address on ``line``, its conversation in ``bridge:line:{address}``.

    An inbound, a ``notify_user`` send, an ``ask``, a park on the thread, a mode override and a
    ``thread``-subject state. Answers the thread and the message ids.
    """
    await world.seed(person(person_id, channel_address(LINE, address)))
    thread = route_thread_id(LINE.route_name, address)
    ids = [
        await world.record(f"{person_id}-in-1", LINE, thread, address),
        await world.notify(address),
        await world.ask(address, f"{person_id}-ask"),
    ]
    await world.park(f"{person_id}-park-thread", thread)
    await world.lua_env.manager.modes.set_mode(thread, "manual")
    world.thread_state(thread)
    return thread, ids


async def test_an_erase_clears_the_route_thread_of_a_single_address_person_it_absorbed(
    world,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    for message_id in q_ids:
        assert (await world.lua_env.store.get_record(message_id)).thread_id == q_thread
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result == {
        "person_id": _P,
        "removed": len(p_ids) + len(q_ids),
        "erased": True,
        "states_erased": 4,
        "absorbed_person_ids": [_Q],
    }
    await assert_clean(
        world,
        _P,
        _Q,
        _P1,
        _P2,
        _Q1,
        *p_ids,
        *q_ids,
        threads=(person_thread_id(_P), q_thread),
        parks=(f"{_P}-park-thread", f"{_P}-park-person", f"{_Q}-park-thread"),
    )

    fresh = await intake_module.accept(
        "twilio",
        LINE_IDENTITY,
        _Q1,
        _Q1,
        "hi",
        "PID-fresh",
        medium_message_id="medium-fresh",
        reply_to_medium_message_id=None,
    )

    [intake] = scheduled
    assert intake.message_id == fresh
    assert intake.thread_id == q_thread
    assert await world.record_ids() == {fresh}
    assert await world.lua_env.store.get_record(fresh) is not None
    newcomer = await world.persons.get_person(
        TARGET, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1
    )
    # The address is unlinked again: the door mints no person, its next turn starts one from nothing.
    assert newcomer is None


async def test_an_erase_clears_each_route_thread_of_an_absorbed_api_address_on_its_own_route(world, monkeypatch):
    await world.routes(LINE, LINE_B, CHAT, CHAT_2)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    address = "alice/u-7"
    await world.seed(person(_Q, api_address(address, CHAT, CHAT_2)))
    threads = (route_thread_id(CHAT.route_name, address), route_thread_id(CHAT_2.route_name, address))
    q_ids = [
        await world.record(f"{_Q}-in-{n}", route, thread, address)
        for n, (route, thread) in enumerate(zip((CHAT, CHAT_2), threads, strict=True))
    ]
    for thread in threads:
        world.thread_state(thread)
    assert (await world.link(_Q, _P)).person_id == _P
    drops: list[tuple[str, str]] = []
    real_drop = world.lua_env.store.drop_thread

    async def _drop_spy(route_name: str, thread_id: str) -> int:
        drops.append((route_name, thread_id))
        return await real_drop(route_name, thread_id)

    monkeypatch.setattr(world.lua_env.store, "drop_thread", _drop_spy)

    result = await ops.delete_conversation_person(_P)

    assert result == {
        "person_id": _P,
        "removed": len(p_ids) + len(q_ids),
        "erased": True,
        "states_erased": 5,
        "absorbed_person_ids": [_Q],
    }
    assert {(CHAT.route_name, threads[0]), (CHAT_2.route_name, threads[1])} <= set(drops)
    assert not {(CHAT_2.route_name, threads[0]), (CHAT.route_name, threads[1])} & set(drops)
    await assert_clean(world, _P, _Q, _P1, _P2, address, *p_ids, *q_ids, threads=(person_thread_id(_P), *threads))


async def _operator_send(world: World, monkeypatch, route, address: str) -> str:
    """An operator's message into ``bridge:{route}:{address}``, its delivery held; answers its id.

    No agent is registered, so no memory line is appended; the erase forgets the thread's
    checkpoint either way."""
    monkeypatch.setattr(operator_send_module, "spawn_delivery", lambda message_id: None)
    monkeypatch.setattr(operator_send_module.accessors, "_agent_registry", dict)
    return await operator_send_module.operator_send(
        route=route,
        thread_id=route_thread_id(route.route_name, address),
        client_address=address,
        text="on it",
        operator_principal="operator-1",
    )


async def test_an_erase_clears_a_thread_only_an_operator_send_wrote_on_another_route(world, monkeypatch):
    await world.routes(LINE, LINE_B, CHAT, CHAT_2)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    address = "alice/u-7"
    await world.seed(person(_Q, api_address(address, CHAT)))
    chat, chat_2 = route_thread_id(CHAT.route_name, address), route_thread_id(CHAT_2.route_name, address)
    q_ids = [
        await world.record(f"{_Q}-in-1", CHAT, chat, address),
        await _operator_send(world, monkeypatch, CHAT_2, address),
    ]
    world.thread_state(chat_2)
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["removed"] == len(p_ids) + len(q_ids)
    assert result["states_erased"] == 4
    assert result["absorbed_person_ids"] == [_Q]
    await assert_clean(world, _P, _Q, _P1, _P2, address, *p_ids, *q_ids, threads=(person_thread_id(_P), chat, chat_2))


async def test_an_erase_clears_a_channel_thread_only_a_send_wrote(world):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await world.seed(person(_Q, channel_address(LINE, _Q1)))
    thread = route_thread_id(LINE.route_name, _Q1)
    q_ids = [await world.notify(_Q1)]
    assert (await world.lua_env.store.get_record(q_ids[0])).thread_id == thread
    world.thread_state(thread)
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["removed"] == len(p_ids) + len(q_ids)
    assert result["states_erased"] == 4
    await assert_clean(world, _P, _Q, _P1, _P2, _Q1, *p_ids, *q_ids, threads=(person_thread_id(_P), thread))


async def _assert_only_records_left(world: World, thread: str, message_ids: list[str]) -> None:
    """``thread`` keeps only its records with their refs, which keep their own retention TTL.

    No writer stamp names it any more."""
    prefix = world.settings.record_key("")
    standing = await world.naming(thread)
    assert {world.settings.record_key(message_id) for message_id in message_ids} <= set(standing)
    strays = {
        key: value
        for key, value in standing.items()
        if not (key.startswith(prefix) or any(message_id in f"{value}" for message_id in message_ids))
    }
    assert strays == {}


async def _deleted_route_world(world: World, monkeypatch) -> tuple[list[str], str, str, list[str]]:
    """P with two addresses; Q's api address wrote on ``chat``, and an operator send alone wrote
    ``bridge:chat-2:{address}``. Answers P's ids, the two threads and Q's ids on ``chat``."""
    await world.routes(LINE, LINE_B, CHAT, CHAT_2)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    address = "alice/u-7"
    await world.seed(person(_Q, api_address(address, CHAT)))
    chat, chat_2 = route_thread_id(CHAT.route_name, address), route_thread_id(CHAT_2.route_name, address)
    q_ids = [await world.record(f"{_Q}-in-1", CHAT, chat, address)]
    return p_ids, chat, chat_2, q_ids


async def _clears_all_but_the_records(world: World, chat_2: str, sent: str, p_ids: list[str], q_ids: list[str]):
    result = await ops.delete_conversation_person(_P)

    assert result["removed"] == len(p_ids) + len(q_ids)
    assert result["absorbed_person_ids"] == [_Q]
    await _assert_only_records_left(world, chat_2, [sent])
    found = await leftovers(world, _P, _Q, *p_ids, *q_ids, threads=(person_thread_id(_P), chat_2), parks=("late",))
    assert set(found) <= {"keys"}
    assert all(sent in f"{key} {value}" or chat_2 in key for key, value in found.get("keys", {}).items())


async def test_a_send_only_thread_on_a_route_deleted_before_the_link_loses_all_but_its_records(world, monkeypatch):
    p_ids, _chat, chat_2, q_ids = await _deleted_route_world(world, monkeypatch)
    sent = await _operator_send(world, monkeypatch, CHAT_2, "alice/u-7")
    await ops.delete_conversation_route(CHAT_2.route_name)
    await world.park("late", chat_2)
    world.thread_state(chat_2)
    assert (await world.link(_Q, _P)).person_id == _P
    assert chat_2 in (await world.persons.read_for_erase(_P)).route_threads

    await _clears_all_but_the_records(world, chat_2, sent, p_ids, q_ids)


async def test_a_send_only_thread_on_a_route_deleted_after_the_link_loses_all_but_its_records(world, monkeypatch):
    p_ids, _chat, chat_2, q_ids = await _deleted_route_world(world, monkeypatch)
    sent = await _operator_send(world, monkeypatch, CHAT_2, "alice/u-7")
    assert (await world.link(_Q, _P)).person_id == _P
    await ops.delete_conversation_route(CHAT_2.route_name)
    await world.park("late", chat_2)
    world.thread_state(chat_2)

    await _clears_all_but_the_records(world, chat_2, sent, p_ids, q_ids)


async def test_a_channel_thread_on_a_route_deleted_before_the_link_loses_all_but_its_records(world):
    old, new = _channel_route("line-w", "+15550007777"), _channel_route("line-w2", "+15550007777")
    await world.routes(LINE, LINE_B, old)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await world.seed(person(_Q, channel_address(old, _Q1)))
    thread = route_thread_id(old.route_name, _Q1)
    inbound = await world.record(f"{_Q}-in-1", old, thread, _Q1)
    await ops.delete_conversation_route(old.route_name)
    await world.routes(new)
    await world.park("late", thread)
    world.thread_state(thread)
    assert (await world.link(_Q, _P)).person_id == _P

    await _clears_all_but_the_records(world, thread, inbound, p_ids, [])


async def test_a_pre_link_thread_nobody_continued_is_cleared_and_a_later_inbound_keys_the_survivor(
    world,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    later = await intake_module.accept(
        "twilio",
        LINE_IDENTITY,
        _Q1,
        _Q1,
        "hi",
        "PID-later",
        medium_message_id="medium-later",
        reply_to_medium_message_id=None,
    )
    [intake] = scheduled
    assert intake.thread_id == person_thread_id(_P)
    # Its turn answered: the record leaves intake, so no turn is in flight at the erase.
    answered = intake.model_copy(
        update={"answer_status": "answered", "answer": "ok", "delivery_status": DeliveryStatus.PENDING_DELIVERY}
    )
    assert await world.lua_env.store.complete_turn(answered) == 1

    assert await _writers(world, q_thread) == {_Q}
    assert q_thread in (await world.persons.read_for_erase(_P)).route_threads

    result = await ops.delete_conversation_person(_P)

    assert result["removed"] == len(p_ids) + len(q_ids) + 1
    await assert_clean(world, _P, _Q, _P1, _P2, _Q1, *p_ids, *q_ids, later, threads=(person_thread_id(_P), q_thread))


async def _emptied_route_thread(world: World) -> str:
    """Q with one address on ``line``, whose inbound chat there was then deleted by a thread delete."""
    await world.seed(person(_Q, channel_address(LINE, _Q1)))
    thread = route_thread_id(LINE.route_name, _Q1)
    await world.record(f"{_Q}-in-1", LINE, thread, _Q1)
    await ops.delete_conversation_thread(LINE.route_name, thread)
    return thread


async def _end_held(world: World, monkeypatch, held, how: str) -> None:
    if how == "failed":
        await held.fail()
    elif how == "timed_out":
        await held.time_out()
    elif how == "settled":
        store = world.lua_env.store
        real_end = store.end_send_record
        lost: list[str] = []

        async def _end_lost_once(record, **kwargs):
            if not lost:
                lost.append(record.message_id)
                raise ConnectionError("the end write was lost")
            return await real_end(record, **kwargs)

        monkeypatch.setattr(store, "end_send_record", _end_lost_once)
        await held.fail()
        assert lost == [held.message_id]
        stalled = await store.get_record(held.message_id)
        assert stalled is not None
        assert stalled.delivery_status is DeliveryStatus.SENDING
        await past_deadline(world.lua_env, held.message_id)
        assert [settled.record.message_id for settled in await store.settle_due_sends()] == [held.message_id]
    else:
        await held.answer(_Q1, LINE_IDENTITY)


@pytest.mark.parametrize("how", ["failed", "timed_out", "settled", "answered"])
async def test_a_send_begun_before_the_link_into_an_empty_thread_is_cleared_wherever_it_ends(world, monkeypatch, how):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread = await _emptied_route_thread(world)
    held = await world.held_send(_Q1, bound=0.3 if how == "timed_out" else 30.0)
    assert (await world.lua_env.store.get_record(held.message_id)).thread_id == thread
    assert (await world.link(_Q, _P)).person_id == _P

    await _end_held(world, monkeypatch, held, how)

    ended = await world.lua_env.store.get_record(held.message_id)
    expected = {
        "failed": DeliveryStatus.FAILED,
        "timed_out": DeliveryStatus.OUTCOME_UNKNOWN,
        "settled": DeliveryStatus.OUTCOME_UNKNOWN,
    }
    if how in expected:
        assert (ended.thread_id, ended.delivery_status) == (thread, expected[how])
        # The end lands in the thread after the link: its writer is the person the address points at now.
        assert await _writers(world, thread) == {_P}
    else:
        assert ended.thread_id == person_thread_id(_P)
        assert await _writers(world, thread) == set()
    assert thread in await _threads_of(world, _Q)

    result = await ops.delete_conversation_person(_P)

    assert result["removed"] == len(p_ids) + 1
    await assert_clean(world, _P, _Q, _P1, _P2, _Q1, *p_ids, held.message_id, threads=(person_thread_id(_P), thread))


_I2 = "+15550003333"
_B = "+15550004444"
_R = "r0000001"
_R1 = "+15550006001"
_M = "m0000001"
_M1, _M2 = "+15550005001", "+15550005002"
_LINE_I2 = LINE.model_copy(update={"our_identity": _I2})
_LINE_W = _channel_route("line-w", LINE_IDENTITY)
_OTHER = ("agent", "echo-2")
_LINE_ON_OTHER = LINE.model_copy(update={"target_name": _OTHER[1]})


def _door(identity: str, address: str, target_name: str = TARGET.target_name) -> str:
    """The writer a twilio write by ``address`` under ``identity`` stamps while nobody holds its door."""
    dak = json.dumps(["twilio", identity, address], separators=(",", ":"))
    return json.dumps([TARGET.target_kind, target_name, dak], separators=(",", ":"))


async def _rewritten_identity(world: World) -> None:
    """``line`` rewritten in place to answer from a second identity."""
    await world.lua_env.manager.put_route(_LINE_I2)


async def _q_chat_on_line(world: World) -> tuple[str, str]:
    await world.seed(person(_Q, channel_address(LINE, _Q1)))
    thread = route_thread_id(LINE.route_name, _Q1)
    return thread, await world.record(f"{_Q}-in-1", LINE, thread, _Q1)


def _i2_inbound(provider_message_id: str):
    return intake_module.accept(
        "twilio",
        _I2,
        _Q1,
        _Q1,
        "hi",
        provider_message_id,
        medium_message_id=f"m-{provider_message_id}",
        reply_to_medium_message_id=None,
    )


def _n_inbound(provider_message_id: str):
    return intake_module.accept(
        "twilio",
        LINE_IDENTITY,
        _Q1,
        _Q1,
        "hi",
        provider_message_id,
        medium_message_id=f"m-{provider_message_id}",
        reply_to_medium_message_id=None,
    )


async def _answered(world: World, scheduled) -> str:  # noqa: F811
    """The newest scheduled turn answered, so it leaves intake; answers its id."""
    intake = scheduled[-1]
    answered = intake.model_copy(
        update={"answer_status": "answered", "answer": "ok", "delivery_status": DeliveryStatus.PENDING_DELIVERY}
    )
    assert await world.lua_env.store.complete_turn(answered) == 1
    return intake.message_id


async def _standing_records(world: World, message_ids: list[str]) -> list[str]:
    return [message_id for message_id in message_ids if await world.lua_env.store.get_record(message_id) is not None]


async def _park_completes(world: World, monkeypatch, thread: str, completion_id: str) -> str:
    """The completion of the turn parked in ``thread``, its delivery held; answers its record id."""
    monkeypatch.setattr(completion_delivery_module, "spawn_delivery", lambda message_id: None)
    with delivery_fire(completion_id):
        out = await completion_delivery_module.deliver_agent_completion(
            thread_id=thread, result="done", completion_id=completion_id, status=PARK_COMPLETION_SUCCEEDED
        )
    assert out == {"message_id": completion_id}
    return completion_id


@pytest.mark.parametrize("begun", ["before_the_link", "after_the_link"])
@pytest.mark.parametrize("ends", ["elsewhere", "elsewhere_after_the_erase", "in_the_thread"])
async def test_a_send_under_an_identity_nobody_maps_keeps_the_thread_once_it_landed_there(world, begun, ends):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    if begun == "after_the_link":
        assert (await world.link(_Q, _P)).person_id == _P
    await _rewritten_identity(world)
    held = await world.held_send(_Q1, our_identity=_I2)
    assert (await world.lua_env.store.get_record(held.message_id)).thread_id == thread
    if begun == "before_the_link":
        assert (await world.link(_Q, _P)).person_id == _P
    assert await _writers(world, thread) == {_Q}
    assert await _threads_of(world, _door(_I2, _Q1)) == {thread}

    if ends == "elsewhere":
        await held.answer(_B, _I2)
    elif ends == "in_the_thread":
        await held.fail()
    result = await ops.delete_conversation_person(_P)
    if ends == "elsewhere_after_the_erase":
        await held.answer(_B, _I2)

    assert result["erased"] is True
    landed = await world.lua_env.store.get_record(held.message_id)
    assert landed.thread_id == (thread if ends == "in_the_thread" else route_thread_id(LINE.route_name, _B))
    if ends == "in_the_thread":
        assert result["removed"] == len(p_ids)
        assert await world.lua_env.store.get_record(chat) is not None
        assert await _writers(world, thread) == {_Q, _door(_I2, _Q1)}
        return
    # A send still sending is in no thread index, so it keeps nothing standing.
    assert result["removed"] == len(p_ids) + 1
    assert await world.lua_env.store.get_record(chat) is None
    assert await _writers(world, thread) == set()


async def _another_person_after_a_thread_delete(world: World) -> tuple[list[str], str, str, HeldSend]:
    """Q's chat on ``line`` forgotten by a thread delete; R (the address under the identity i2) then
    writes the thread, with a park and a state on it, and a send to Q begins into it. Answers P's ids,
    the thread, R's record and Q's send."""
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, _chat = await _q_chat_on_line(world)
    await ops.delete_conversation_thread(LINE.route_name, thread)
    assert await _writers(world, thread) == set()
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(_R, channel_address(_LINE_I2, _Q1)))
    r_chat = await world.record(f"{_R}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    await world.park(f"{_R}-park", thread)
    world.thread_state(thread)
    await world.lua_env.manager.put_route(LINE)
    held = await world.held_send(_Q1)
    begun = await world.lua_env.store.get_record(held.message_id)
    assert begun is not None
    assert begun.thread_id == thread
    return p_ids, thread, r_chat, held


async def test_a_thread_another_person_wrote_in_after_a_thread_delete_is_left(world):
    p_ids, thread, r_chat, held = await _another_person_after_a_thread_delete(world)
    assert (await world.link(_Q, _P)).person_id == _P
    assert await _writers(world, thread) == {_R}
    assert thread in await _threads_of(world, _Q)
    await held.answer(_B, LINE_IDENTITY)
    forgotten_before = len(world.saver.deleted)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids)
    assert await world.lua_env.store.get_record(r_chat) is not None
    assert thread not in world.saver.deleted[forgotten_before:]
    assert await world.park_stands(f"{_R}-park")
    assert world.states_naming(thread) != []
    assert await _writers(world, thread) == {_R}


async def test_a_thread_another_person_wrote_in_is_wiped_once_that_person_is_linked_too(world):
    p_ids, thread, r_chat, held = await _another_person_after_a_thread_delete(world)
    assert (await world.link(_Q, _P)).person_id == _P
    await held.answer(_B, LINE_IDENTITY)

    assert (await world.link(_R, _P)).person_id == _P

    assert (await world.persons.read_for_erase(_P)).absorbed_ids == [_Q, _R]
    assert await _writers(world, thread) == {_R}
    forgotten_before = len(world.saver.deleted)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_Q, _R]
    assert result["removed"] == len(p_ids) + 1
    assert await world.lua_env.store.get_record(r_chat) is None
    assert thread in world.saver.deleted[forgotten_before:]
    assert not await world.park_stands(f"{_R}-park")
    assert world.states_naming(thread) == []
    assert await _writers(world, thread) == set()


async def _q_and_r_on_one_address(world: World) -> tuple[list[str], str]:
    """Q holds the address on ``line``, R the same address on the identity i2, which ``line`` now
    answers from. Answers P's ids and the address's thread on ``line``."""
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await world.seed(person(_Q, channel_address(LINE, _Q1)))
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(_R, channel_address(_LINE_I2, _Q1)))
    return p_ids, route_thread_id(LINE.route_name, _Q1)


async def test_a_write_inside_a_thread_delete_goes_with_it(
    world,
    monkeypatch,
    scheduled,  # noqa: F811
):
    p_ids, thread = await _q_and_r_on_one_address(world)
    await world.record(f"{_R}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    dropping = pause_call(monkeypatch, world.lua_env.store, "drop_thread")
    deleted = asyncio.ensure_future(ops.delete_conversation_thread(LINE.route_name, thread))
    await asyncio.wait_for(dropping.reached.wait(), 10)
    await _i2_inbound("PID-R")
    assert await _writers(world, thread) == {_R}
    dropping.go.set()
    await deleted
    assert await _writers(world, thread) == set()

    await world.lua_env.manager.put_route(LINE)
    q_chat = await world.record(f"{_Q}-in-1", LINE, thread, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    forgotten_before = len(world.saver.deleted)

    result = await ops.delete_conversation_person(_P)

    assert result["removed"] == len(p_ids) + 1
    assert thread in world.saver.deleted[forgotten_before:]
    r_visits = world.settings.writer_threads_key(_R)
    assert await leftovers(world, _Q, q_chat, threads=(thread,)) == {"keys": {r_visits: [thread]}}


async def test_a_write_its_redrive_drops_as_a_duplicate_keeps_nothing_standing(
    world,
    scheduled,  # noqa: F811
):
    p_ids, thread = await _q_and_r_on_one_address(world)
    await _i2_inbound("PID-R")
    [intake] = scheduled
    await world.redis.set(world.settings.dedupe_key("twilio", "PID-R"), "another-attempt")
    await redrive_module._arbitrate_stranded_intake(world.lua_env.store, intake)
    assert await world.lua_env.store.get_record(intake.message_id) is None
    assert await _writers(world, thread) == set()

    await world.lua_env.manager.put_route(LINE)
    q_chat = await world.record(f"{_Q}-in-1", LINE, thread, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 1
    assert await world.lua_env.store.get_record(q_chat) is None
    assert await _writers(world, thread) == set()


async def _unlinked_after_the_link(world: World) -> tuple[list[str], str, list[str], str]:
    """Q's one address on ``line`` linked into P, then unlinked from P to a fresh person N; answers N's id last."""
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    detached = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1)
    assert detached.person_id not in (_P, _Q)
    return p_ids, q_thread, q_ids, detached.person_id


async def _assert_left_for_n(
    world: World, n_id: str, thread: str, message_ids: list[str], forgotten_before: int
) -> None:
    for message_id in message_ids:
        record = await world.lua_env.store.get_record(message_id)
        assert record is not None
        assert record.thread_id == thread
    assert thread not in world.saver.deleted[forgotten_before:]
    assert await world.park_stands(f"{_Q}-park-thread")
    assert world.states_naming(thread) != []
    assert await _writers(world, thread) == {_Q, n_id}
    address = channel_address(LINE, _Q1)
    assert (await world.index())[world.index_field(address)] == n_id
    n = await world.persons.get_person(
        TARGET, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1
    )
    assert n is not None
    assert n.person_id == n_id


@pytest.mark.parametrize("n_writes", ["before_the_erase", "never", "while_the_erase_runs", "by_a_send_begun_to_it"])
async def test_a_thread_a_fresh_person_continued_after_an_unlink_is_left(
    world,
    monkeypatch,
    scheduled,  # noqa: F811
    n_writes,
):
    p_ids, q_thread, q_ids, n_id = await _unlinked_after_the_link(world)
    n_ids: list[str] = []
    held = None
    if n_writes == "before_the_erase":
        n_ids.append(await _n_inbound("PID-N"))
        [intake] = scheduled
        assert intake.thread_id == q_thread
        await _answered(world, scheduled)
    elif n_writes == "by_a_send_begun_to_it":
        held = await world.held_send(_Q1)
        n_ids.append(held.message_id)
    forgotten_before = len(world.saver.deleted)

    if n_writes == "while_the_erase_runs":
        cleared = pause_call(
            monkeypatch,
            threads_delete_module,
            "_clear_route_thread",
            when=lambda _person, _erased, thread, _caps: thread == q_thread,
        )
        erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
        await asyncio.wait_for(cleared.reached.wait(), 10)
        # N's turn is still at intake when the erase reaches the thread: N is a writer of it now, so it is left.
        n_ids.append(await _n_inbound("PID-N"))
        assert [intake.thread_id for intake in scheduled] == [q_thread]
        assert await world.lua_env.store.thread_has_live_intake(q_thread)
        cleared.go.set()
        result = await asyncio.wait_for(erase, 30)
    else:
        result = await ops.delete_conversation_person(_P)
    if held is not None:
        await held.answer(_Q1, LINE_IDENTITY)

    assert result["absorbed_person_ids"] == [_Q]
    assert result["erased"] is True
    if n_writes in ("never", "by_a_send_begun_to_it"):
        # A send still sending is in no thread index, so it keeps nothing standing; it is forgotten
        # when it lands in the cleared thread.
        assert result["removed"] == len(p_ids) + len(q_ids)
        assert q_thread in world.saver.deleted[forgotten_before:]
        n_visits = {"keys": {world.settings.writer_threads_key(n_id): [q_thread]}} if n_ids else {}
        assert await leftovers(world, *q_ids, *n_ids, threads=(q_thread,), parks=(f"{_Q}-park-thread",)) == n_visits
        assert world.states_naming(q_thread) == []
        return
    assert result["removed"] == len(p_ids)
    await _assert_left_for_n(world, n_id, q_thread, [*q_ids, *n_ids], forgotten_before)


@pytest.mark.parametrize("moved_by", ["rewrite", "delete_and_create", "rewrite_with_no_write"])
async def test_a_thread_continued_after_its_route_moved_to_another_target_is_left(
    world,
    scheduled,  # noqa: F811
    moved_by,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    if moved_by == "delete_and_create":
        await ops.delete_conversation_route(LINE.route_name)
        await world.park(f"{_Q}-park-thread", q_thread)
        world.thread_state(q_thread)
    await world.lua_env.manager.put_route(_LINE_ON_OTHER)
    if moved_by == "rewrite_with_no_write":
        assert await _writers(world, q_thread) == {_Q}

        result = await ops.delete_conversation_person(_P)

        assert result["removed"] == len(p_ids) + len(q_ids)
        assert await leftovers(world, *q_ids, threads=(q_thread,), parks=(f"{_Q}-park-thread",)) == {}
        assert world.states_naming(q_thread) == []
        return

    later = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-Y", medium_message_id="m-PID-Y", reply_to_medium_message_id=None
    )
    [intake] = scheduled
    assert intake.thread_id == q_thread
    await _answered(world, scheduled)
    # The other target holds no person for the address: its write stamps the door.
    on_other = _door(LINE_IDENTITY, _Q1, _OTHER[1])
    # A route delete leaves its threads' records unindexed.
    q_writers = set() if moved_by == "delete_and_create" else {_Q}
    assert await _writers(world, q_thread) == {*q_writers, on_other}
    world.states_pg.seed_record(STATE, *_OTHER, "thread", q_thread, {"n": 2})
    standing = await _standing_records(world, [*q_ids, later])
    p_standing = await _standing_records(world, p_ids)
    forgotten_before = len(world.saver.deleted)

    result = await ops.delete_conversation_person(_P)

    assert result["absorbed_person_ids"] == [_Q]
    # A route delete leaves its threads' records to their TTL, unindexed: the erase counts what it removed.
    assert result["removed"] == len(p_standing) - len(await _standing_records(world, p_standing))
    if moved_by == "rewrite":
        assert result["removed"] == len(p_ids)
    assert await _standing_records(world, [*q_ids, later]) == standing
    assert later in standing
    assert q_thread not in world.saver.deleted[forgotten_before:]
    assert await world.park_stands(f"{_Q}-park-thread")
    assert sorted(row[1:3] for row in world.states_naming(q_thread)) == sorted([_TARGET_KEY, _OTHER])
    assert await _writers(world, q_thread) == {*q_writers, on_other}


async def _p_left_with_only(world: World, scheduled, thread: str) -> tuple[str, str]:  # noqa: F811
    """P's two own addresses unlinked, a write from P in ``thread`` from its one address left, then
    P's first address linked back; answers the write's id and the person P's first address was."""
    first = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_P1)
    await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_B_IDENTITY, address=_P2)
    await _n_inbound("PID-P")
    written = await _answered(world, scheduled)
    record = await world.lua_env.store.get_record(written)
    assert record is not None
    assert record.thread_id == thread
    assert (await world.link(first.person_id, _P)).person_id == _P
    return written, first.person_id


@pytest.mark.parametrize("order", ["p_first", "m_first"])
@pytest.mark.parametrize("step", ["park_completion", "operator_send", "p_write"])
async def test_a_write_after_the_link_stamps_the_survivor_and_a_later_link_of_the_address_leaves_it(
    world,
    monkeypatch,
    scheduled,  # noqa: F811
    step,
    order,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    assert await _writers(world, q_thread) == {_Q}
    absorbed = [_Q]
    if step == "park_completion":
        q_ids.append(await _park_completes(world, monkeypatch, q_thread, "q-park-done"))
    elif step == "operator_send":
        q_ids.append(await _operator_send(world, monkeypatch, LINE, _Q1))
    else:
        written, first = await _p_left_with_only(world, scheduled, q_thread)
        q_ids.append(written)
        p_ids = await _standing_records(world, p_ids)
        absorbed = sorted([_Q, first])
    assert await _writers(world, q_thread) == {_Q, _P}
    n = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1)
    m_ids = await _two_address_person(world, _M, _M1, _M2, age_days=2)
    assert (await world.link(n.person_id, _M)).person_id == _M
    assert q_thread not in (await world.persons.read_for_erase(_M)).route_threads

    if order == "m_first":
        m_result = await ops.delete_conversation_person(_M)
        assert m_result["absorbed_person_ids"] == [n.person_id]
        assert m_result["removed"] == len(m_ids)
        for message_id in q_ids:
            assert (await world.lua_env.store.get_record(message_id)).thread_id == q_thread
        assert await _writers(world, q_thread) == {_Q, _P}

    result = await ops.delete_conversation_person(_P)

    assert result["absorbed_person_ids"] == absorbed
    assert result["removed"] == len(p_ids) + len(q_ids)
    assert world.states_naming(q_thread) == []
    assert q_thread in world.saver.deleted
    if order == "p_first":
        m_result = await ops.delete_conversation_person(_M)
        assert m_result["erased"] is True
        assert m_result["removed"] == len(m_ids)
    assert await leftovers(world, *q_ids, threads=(q_thread,), parks=(f"{_Q}-park-thread",)) == {}


async def test_a_thread_two_survivors_wrote_in_is_left_by_the_first_erase_and_wiped_by_the_second(
    world,
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    q_ids.append(await _park_completes(world, monkeypatch, q_thread, "q-park-done"))
    n = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1)
    m_ids = await _two_address_person(world, _M, _M1, _M2, age_days=2)
    assert (await world.link(n.person_id, _M)).person_id == _M
    # The address's index field names M now: the write stamps M.
    later = await _operator_send(world, monkeypatch, LINE, _Q1)
    assert await _writers(world, q_thread) == {_Q, _P, _M}

    p_result = await ops.delete_conversation_person(_P)

    assert p_result["absorbed_person_ids"] == [_Q]
    assert p_result["removed"] == len(p_ids)
    for message_id in [*q_ids, later]:
        assert (await world.lua_env.store.get_record(message_id)).thread_id == q_thread
    assert await world.park_stands(f"{_Q}-park-thread")
    assert await _writers(world, q_thread) == {_Q, _P, _M}

    m_result = await ops.delete_conversation_person(_M)

    assert m_result["absorbed_person_ids"] == [n.person_id]
    assert m_result["removed"] == len(m_ids) + len(q_ids) + 1
    assert await leftovers(world, *q_ids, later, threads=(q_thread,), parks=(f"{_Q}-park-thread",)) == {}
    assert world.states_naming(q_thread) == []


async def _shared_before_the_link(world: World) -> tuple[list[str], str, list[str], str, list[str]]:
    """Q's one address chats in ``bridge:line:{a}``; ``line`` moves to the identity i2, and Q's address
    then writes ``bridge:line-w:{a}`` on a new route under the first identity. Answers P's ids, the
    thread with Q's ids in it, and Q's other thread with its ids."""
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.lua_env.manager.put_route(_LINE_W)
    other = route_thread_id(_LINE_W.route_name, _Q1)
    other_ids = [await world.record(f"{_Q}-in-w", _LINE_W, other, _Q1)]
    await world.park(f"{_Q}-park-w", other)
    world.thread_state(other)
    return p_ids, q_thread, q_ids, other, other_ids


@pytest.mark.parametrize("landed", ["before_the_erase", "while_it_waits"])
async def test_a_thread_another_person_wrote_in_before_the_link_is_left(
    world,
    monkeypatch,
    scheduled,  # noqa: F811
    landed,
):
    p_ids, q_thread, q_ids, other, other_ids = await _shared_before_the_link(world)
    if landed == "before_the_erase":
        i2_chat = await _i2_inbound("PID-I2")
        assert (await world.lua_env.store.get_record(i2_chat)).thread_id == q_thread
        assert (await world.link(_Q, _P)).person_id == _P
        assert await _writers(world, q_thread) == {_Q, _door(_I2, _Q1)}
        # The other writer's turn is still at intake: the erase leaves the thread, so that turn refuses nothing.
        assert await world.lua_env.store.thread_has_live_intake(q_thread)
        result = await ops.delete_conversation_person(_P)
    else:
        reads = []
        real_read = world.persons.read_for_erase

        async def _read_spy(person_id):
            reads.append(await real_read(person_id))
            return reads[-1]

        monkeypatch.setattr(world.persons, "read_for_erase", _read_spy)
        marking = pause_call(monkeypatch, world.persons, "mark_erasing")
        erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
        await asyncio.wait_for(marking.reached.wait(), 10)
        i2_chat = await _i2_inbound("PID-I2")
        await _answered(world, scheduled)
        assert (await world.link(_Q, _P)).person_id == _P
        marking.go.set()
        result = await asyncio.wait_for(erase, 30)
        first, after_the_mark, _before_the_final_step = reads
        assert first.absorbed_ids == []
        assert after_the_mark.absorbed_ids == [_Q]
        assert q_thread in after_the_mark.route_threads

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_Q]
    assert result["removed"] == len(p_ids) + len(other_ids)
    # P's person and aggregated-thread state, Q's person state and Q's other thread's.
    assert result["states_erased"] == 4
    assert await leftovers(world, *other_ids, threads=(other,), parks=(f"{_Q}-park-w",)) == {}
    for message_id in [*q_ids, i2_chat]:
        assert (await world.lua_env.store.get_record(message_id)).thread_id == q_thread
    assert q_thread not in world.saver.deleted
    assert await world.park_stands(f"{_Q}-park-thread")
    assert world.states_naming(q_thread) != []
    assert await _writers(world, q_thread) == {_Q, _door(_I2, _Q1)}


@pytest.mark.parametrize("added_by", ["park_completion_on_another_target", "write_under_the_survivors_other_address"])
async def test_a_write_after_the_link_stamps_the_writer_its_target_names(
    world,
    monkeypatch,
    added_by,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    if added_by == "write_under_the_survivors_other_address":
        # P also has the address on the identity i2.
        await world.seed(
            person(
                _P,
                channel_address(LINE, _P1),
                channel_address(LINE_B, _P2),
                channel_address(_LINE_I2, _Q1),
                age_days=1,
            ),
            state=False,
        )
    q_thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert await _writers(world, q_thread) == {_Q}
    assert (await world.link(_Q, _P)).person_id == _P
    if added_by == "park_completion_on_another_target":
        await world.lua_env.manager.put_route(_LINE_ON_OTHER)
        q_ids.append(await _park_completes(world, monkeypatch, q_thread, "q-park-done"))
        added = _door(LINE_IDENTITY, _Q1, _OTHER[1])
    else:
        await world.lua_env.manager.put_route(_LINE_I2)
        q_ids.append(await world.record("p-in-i2", _LINE_I2, q_thread, _Q1, our_identity=_I2))
        added = _P
    assert await _writers(world, q_thread) == {_Q, added}

    result = await ops.delete_conversation_person(_P)

    if added == _P:
        assert result["removed"] == len(p_ids) + len(q_ids)
        assert q_thread in world.saver.deleted
        assert await leftovers(world, *q_ids, threads=(q_thread,), parks=(f"{_Q}-park-thread",)) == {}
        assert world.states_naming(q_thread) == []
        return
    assert result["removed"] == len(p_ids)
    assert await _standing_records(world, q_ids) == q_ids
    assert q_thread not in world.saver.deleted
    assert await _writers(world, q_thread) == {_Q, added}


async def _two_writers_before_any_link(world: World) -> tuple[list[str], str, list[str]]:
    """Q (an address on ``line``) and R (the same address once ``line`` answers from i2) both write
    ``bridge:line:{a}`` before any link; ``line`` is then back on its first identity."""
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=2)
    await world.seed(person(_Q, channel_address(LINE, _Q1), age_days=1))
    thread = route_thread_id(LINE.route_name, _Q1)
    ids = [await world.record(f"{_Q}-in-1", LINE, thread, _Q1)]
    await world.park(f"{_Q}-park", thread)
    world.thread_state(thread)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(_R, channel_address(_LINE_I2, _Q1)))
    ids.append(await world.record(f"{_R}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2))
    await world.lua_env.manager.put_route(LINE)
    assert await _writers(world, thread) == {_Q, _R}
    return p_ids, thread, ids


@pytest.mark.parametrize("order", ["q_then_r", "r_then_q"])
async def test_a_thread_two_absorbed_persons_wrote_before_their_links_is_wiped(world, order):
    p_ids, thread, ids = await _two_writers_before_any_link(world)
    first, second = (_Q, _R) if order == "q_then_r" else (_R, _Q)
    assert (await world.link(first, _P)).person_id == _P
    assert (await world.link(second, _P)).person_id == _P
    assert (await world.persons.read_for_erase(_P)).absorbed_ids == [_Q, _R]

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_Q, _R]
    assert result["removed"] == len(p_ids) + len(ids)
    assert await leftovers(world, _Q, _R, *ids, threads=(thread,), parks=(f"{_Q}-park",)) == {}
    assert world.states_naming(thread) == []


async def test_a_thread_an_unlinked_absorbed_address_wrote_before_its_link_is_wiped(world):
    z = "z0000001"
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=2)
    await world.seed(person(_Q, channel_address(LINE, _Q1), age_days=1))
    thread = route_thread_id(LINE.route_name, _Q1)
    ids = [await world.record(f"{_Q}-in-1", LINE, thread, _Q1)]
    await world.park(f"{_Q}-park", thread)
    world.thread_state(thread)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(z, channel_address(_LINE_I2, _Q1)))
    ids.append(await world.record(f"{z}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2))
    assert (await world.link(z, _P)).person_id == _P
    # Z's address leaves P and never writes again.
    detached = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=_I2, address=_Q1)
    assert detached.person_id not in (_P, _Q, z)
    await world.lua_env.manager.put_route(LINE)
    assert (await world.link(_Q, _P)).person_id == _P
    assert await _writers(world, thread) == {_Q, z}

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_Q, z]
    assert result["removed"] == len(p_ids) + len(ids)
    assert await leftovers(world, _Q, z, *ids, threads=(thread,), parks=(f"{_Q}-park",)) == {}
    assert world.states_naming(thread) == []


async def test_a_chain_of_links_brings_every_writer_of_the_thread_into_the_erase(world):
    b, d, e, s = "b0000001", "d0000001", "e0000001", "s0000001"
    i3 = "+15550007777"
    line_i3 = LINE.model_copy(update={"our_identity": i3})
    await world.routes(LINE, LINE_B)
    s_ids = await _two_address_person(world, s, _P1, _P2, age_days=4)
    thread = route_thread_id(LINE.route_name, _Q1)
    await world.seed(person(b, channel_address(LINE, _Q1), age_days=1))
    ids = [await world.record("b-in-1", LINE, thread, _Q1)]
    await world.park("b-park", thread)
    world.thread_state(thread)
    await world.lua_env.manager.put_route(line_i3)
    await world.seed(person(d, channel_address(line_i3, _Q1), age_days=3))
    ids.append(await world.record("d-in-1", line_i3, thread, _Q1, our_identity=i3))
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(e, channel_address(_LINE_I2, _Q1), age_days=2))
    ids.append(await world.record("e-in-1", _LINE_I2, thread, _Q1, our_identity=_I2))
    await world.lua_env.manager.put_route(LINE)
    assert await _writers(world, thread) == {b, d, e}
    assert (await world.link(e, d)).person_id == d
    assert (await world.link(d, s)).person_id == s

    # B into S last: every writer of the thread is S's now.
    assert (await world.link(b, s)).person_id == s

    assert (await world.persons.read_for_erase(s)).absorbed_ids == [b, d, e]
    result = await ops.delete_conversation_person(s)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [b, d, e]
    assert result["removed"] == len(s_ids) + len(ids)
    assert await leftovers(world, b, d, e, *ids, threads=(thread,), parks=("b-park",)) == {}
    assert world.states_naming(thread) == []


async def test_a_thread_a_fresh_person_continued_is_wiped_once_that_person_is_linked_back(
    world,
    scheduled,  # noqa: F811
):
    p_ids, q_thread, q_ids, n_id = await _unlinked_after_the_link(world)
    n_ids = [await _n_inbound("PID-N")]
    await _answered(world, scheduled)
    assert await _writers(world, q_thread) == {_Q, n_id}
    assert (await world.link(n_id, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == sorted([_Q, n_id])
    assert result["removed"] == len(p_ids) + len(q_ids) + len(n_ids)
    assert await leftovers(world, *q_ids, *n_ids, threads=(q_thread,), parks=(f"{_Q}-park-thread",)) == {}
    assert world.states_naming(q_thread) == []


async def _fresh_writer_on_line(world: World, absorber: str) -> tuple[str, str, str]:
    """Q (Q1 under ``line``'s identity, written only on ``line-w``) linked into ``absorber``, then Q1
    unlinked into a fresh person N. Answers N's id, Q's ``line-w`` thread and its message id;
    ``line-w`` is gone and ``line`` is back on its first identity."""
    await world.lua_env.manager.put_route(_LINE_W)
    await world.seed(person(_Q, channel_address(_LINE_W, _Q1), age_days=1))
    w_thread = route_thread_id(_LINE_W.route_name, _Q1)
    q_chat = await world.record(f"{_Q}-in-w", _LINE_W, w_thread, _Q1)
    assert (await world.link(_Q, absorber)).person_id == absorber
    detached = await world.persons.detach(
        absorber, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1
    )
    assert detached.person_id not in (_P, _Q, absorber)
    await world.lua_env.manager.delete_route(_LINE_W.route_name)
    await world.lua_env.manager.put_route(LINE)
    return detached.person_id, w_thread, q_chat


async def _left_to_n(world: World, n_id: str, thread: str, message_ids: list[str], *, erased: str) -> None:
    """``thread`` stands with ``message_ids`` in it, written by N and by the erased id ``erased``."""
    for message_id in message_ids:
        record = await world.lua_env.store.get_record(message_id)
        assert record is not None
        assert record.thread_id == thread
    assert world.states_naming(thread) != []
    assert await _writers(world, thread) == {erased, n_id}


async def test_a_fresh_person_on_an_absorbed_address_keeps_a_thread_its_old_holder_never_wrote_in(world):
    a = "a0000001"
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=3)
    thread = route_thread_id(LINE.route_name, _Q1)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(a, channel_address(_LINE_I2, _Q1), age_days=2))
    a_chat = await world.record(f"{a}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    world.thread_state(thread)
    n_id, w_thread, q_chat = await _fresh_writer_on_line(world, _P)
    assert (await world.persons.read_for_erase(_P)).route_threads == [w_thread]
    # N writes, under the address Q held, in a thread Q never wrote in.
    n_chat = await world.record(f"{n_id}-in-1", LINE, thread, _Q1)

    assert (await world.link(a, _P)).person_id == _P

    assert await _writers(world, thread) == {a, n_id}
    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [a, _Q]
    assert result["removed"] == len(p_ids) + 1
    assert await world.lua_env.store.get_record(q_chat) is None
    await _left_to_n(world, n_id, thread, [a_chat, n_chat], erased=a)


async def test_a_fresh_persons_write_leaves_the_thread_when_its_old_holder_comes_in_by_a_chain(world):
    r, x = "r0000002", "x0000001"
    r1 = "+15550008001"
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=3)
    await world.seed(person(r, channel_address(LINE_B, r1), age_days=2))
    n_id, w_thread, q_chat = await _fresh_writer_on_line(world, r)
    thread = route_thread_id(LINE.route_name, _Q1)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(x, channel_address(_LINE_I2, _Q1)))
    x_chat = await world.record(f"{x}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    world.thread_state(thread)
    await world.lua_env.manager.put_route(LINE)
    n_chat = await world.record(f"{n_id}-in-1", LINE, thread, _Q1)
    assert (await world.link(x, _P)).person_id == _P

    # R brings Q, which held N's address but wrote only in its ``line-w`` thread.
    assert (await world.link(r, _P)).person_id == _P

    assert (await world.persons.read_for_erase(_P)).route_threads == sorted([thread, w_thread])
    assert await _writers(world, thread) == {x, n_id}
    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == sorted([_Q, r, x])
    assert result["removed"] == len(p_ids) + 1
    assert await world.lua_env.store.get_record(q_chat) is None
    await _left_to_n(world, n_id, thread, [x_chat, n_chat], erased=x)
