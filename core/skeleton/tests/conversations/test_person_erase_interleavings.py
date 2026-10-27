"""Person erases that overlap one another and the writes, links, unlinks and thread deletes around them.

An erase sets its mark before it decides on any route thread, and decides each thread under its
hold from the ``writer`` its standing records were written with, judged against the person rows,
the erase marks and the absorbed-into pointers at that moment. A thread it leaves is not written
to, so of any erases of the writers of one chat, the one that looks last wipes it.
"""

from __future__ import annotations

import asyncio
import itertools
import re

import pytest
from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission, ConversationRoute

from tai42_skeleton.conversations.persons import PairingTarget
from tai42_skeleton.conversations.turn import event_door as event_door_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module
from tai42_skeleton.operations.errors import ConflictError

from .conftest import _tool_channel_route, pause_call
from .linked_erase import (
    CHAT,
    CHAT_2,
    LINE,
    LINE_B,
    LINE_IDENTITY,
    STATE,
    TARGET,
    World,
    api_address,
    channel_address,
    person,
    route_thread_id,
)
from .test_person_erase_absorbed import _answered, _operator_send
from .test_person_erase_sends import scheduled  # noqa: F401  # the doors' turn scheduling, held
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture
from .test_person_erase_writers import (  # noqa: F401  # the linked-erase world fixture
    _I2,
    _P,
    _Q,
    _Q1,
    _X,
    _Y,
    _Y1,
    _Z,
    _Z1,
    _records_standing,
    _threads_of,
    world,
)

_I3 = "+15550009999"
_X2 = "+15550006001"
_API = "alice/u-7"
_EVENT_ROUTE = _tool_channel_route(our_identity="+15550007777")
_LATE_WRITES = ("inbound", "operator_send", "event", "completion_landing", "end_landing")
# An operator send waits for the thread's turn slot, which the erase holds while it clears the
# thread, so it lands only once the hold is let go.
_IN_THE_HOLD = ("inbound", "event", "completion_landing", "end_landing")
# An event names a thread that has records, so none lands in a thread the erase dropped.
_AFTER_THE_DROP = [
    *(("after_the_drop", write) for write in ("inbound", "completion_landing", "end_landing")),
    *(("before_the_final_step", write) for write in ("inbound", "operator_send", "completion_landing", "end_landing")),
]


def _on(route: ConversationRoute, identity: str) -> ConversationRoute:
    return route.model_copy(update={"our_identity": identity})


def _route_of(write: str) -> ConversationRoute:
    return _EVENT_ROUTE if write == "event" else LINE


async def _writer(world: World, message_id: str) -> str | None:  # noqa: F811
    return await world.redis.hget(world.settings.keyspace.record_key(message_id), "writer")


async def _write(world: World, route: ConversationRoute, person_id: str, identity: str, *, age_days: int) -> str:  # noqa: F811
    """``person_id`` holds ``_Q1`` under ``identity`` on ``route``'s target and writes once into
    ``route``'s thread of ``_Q1``; ``route`` answers from its own identity again afterwards.
    Answers the record's id."""
    line = _on(route, identity)
    await world.lua_env.manager.put_route(line)
    holder = person(person_id, channel_address(line, _Q1), age_days=age_days)
    await world.seed(holder.model_copy(update={"target_kind": route.target_kind, "target_name": route.target_name}))
    record = await world.record(
        f"{person_id}-in-{age_days}", line, route_thread_id(route.route_name, _Q1), _Q1, our_identity=identity
    )
    await world.lua_env.manager.put_route(route)
    return record


async def _chat(world: World, route: ConversationRoute, *others: tuple[str, str]) -> tuple[str, list[str]]:  # noqa: F811
    """``route``'s thread of ``_Q1``, written by ``_Q`` under the route's identity and by each
    ``(person_id, identity)`` of ``others``; answers the thread and the record ids."""
    await world.routes(LINE, LINE_B)
    await world.lua_env.manager.put_route(route)
    assert route.our_identity is not None
    ids = [await _write(world, route, _Q, route.our_identity, age_days=1)]
    for age, (person_id, identity) in enumerate(others, start=2):
        ids.append(await _write(world, route, person_id, identity, age_days=age))
    thread = route_thread_id(route.route_name, _Q1)
    world.states_pg.seed_record(STATE, route.target_kind, route.target_name, "thread", thread, {"n": 1})
    return thread, ids


async def _late(world: World, monkeypatch, scheduled, write: str, thread: str) -> str:  # noqa: F811
    """One write by ``write``'s door from or to ``_Q1`` into ``thread``; answers the record's id."""
    if write == "inbound":
        return await world.record(f"late-{len(await world.record_ids())}", LINE, thread, _Q1)
    if write == "operator_send":
        return await _operator_send(world, monkeypatch, LINE, _Q1)
    if write == "event":

        async def _connected() -> bool:
            return True

        await event_door_module.submit_event(
            _EVENT_ROUTE.route_name,
            ConversationEventSubmission(
                thread_id=thread,
                event=ConversationEvent(
                    event_id=f"evt-{len(await world.record_ids())}", kind="provider.update", payload={}
                ),
            ),
            "svc-int",
            client_connected=_connected,
        )
        return await _answered(world, scheduled)
    held = await world.held_send(_Q1)
    if write == "completion_landing":
        await held.answer(_Q1, LINE_IDENTITY)
    else:
        await held.fail()
    return held.message_id


def _erase_of(who: str):
    return lambda erased, *_args: erased.person_id == who


def _deciding(who: str, thread: str):
    return lambda erased, _ids, decided, _caps: erased.person_id == who and decided == thread


# -- overlapping erases of the writers of one chat --------------------------------------------


def _paused_at(monkeypatch, world: World, point: str, who: str, thread: str):  # noqa: F811
    if point == "after_the_mark":
        return pause_call(monkeypatch, world.persons, "mark_erasing", after=True, when=lambda marked: marked == who)
    if point == "after_the_decision":
        return pause_call(
            monkeypatch, threads_delete_module, "_clear_route_thread", after=True, when=_deciding(who, thread)
        )
    return pause_call(monkeypatch, world.persons, "erase", when=_erase_of(who))


@pytest.mark.parametrize("point", ["after_the_mark", "after_the_decision", "before_the_final_step"])
@pytest.mark.parametrize(("first", "second"), [(_Q, _X), (_X, _Q)])
async def test_two_overlapping_erases_of_both_writers_wipe_their_chat_wherever_the_first_pauses(
    world,  # noqa: F811
    monkeypatch,
    point,
    first,
    second,
):
    thread, ids = await _chat(world, LINE, (_X, _I2))
    paused = _paused_at(monkeypatch, world, point, first, thread)
    first_erase = asyncio.ensure_future(ops.delete_conversation_person(first))
    await asyncio.wait_for(paused.reached.wait(), 10)

    second_result = await ops.delete_conversation_person(second)
    paused.go.set()
    first_result = await asyncio.wait_for(first_erase, 30)

    assert (first_result["erased"], second_result["erased"]) == (True, True)
    assert await _records_standing(world, ids) == []
    assert world.states_naming(thread) == []
    assert await _threads_of(world, _Q) == await _threads_of(world, _X) == set()


@pytest.mark.parametrize("order", list(itertools.permutations([_Q, _X, _Z])))
async def test_three_overlapping_erases_of_every_writer_wipe_their_chat_in_every_order(world, monkeypatch, order):  # noqa: F811
    thread, ids = await _chat(world, LINE, (_X, _I2), (_Z, _I3))
    first, second, third = order
    one = pause_call(monkeypatch, world.persons, "erase", when=_erase_of(first))
    two = pause_call(monkeypatch, world.persons, "erase", when=_erase_of(second))
    first_erase = asyncio.ensure_future(ops.delete_conversation_person(first))
    await asyncio.wait_for(one.reached.wait(), 10)
    second_erase = asyncio.ensure_future(ops.delete_conversation_person(second))
    await asyncio.wait_for(two.reached.wait(), 10)

    results = [await ops.delete_conversation_person(third)]
    one.go.set()
    two.go.set()
    results += [await asyncio.wait_for(first_erase, 30), await asyncio.wait_for(second_erase, 30)]

    assert [result["erased"] for result in results] == [True, True, True]
    assert await _records_standing(world, ids) == []
    assert world.states_naming(thread) == []


# -- writes from or to the address of the person being erased ---------------------------------


@pytest.mark.parametrize("write", _LATE_WRITES)
async def test_a_write_into_a_chat_the_erase_left_goes_with_the_other_writers_erase(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    write,
):
    thread, ids = await _chat(world, _route_of(write), (_X, _I2))
    paused = pause_call(monkeypatch, world.persons, "erase", when=_erase_of(_Q))
    q_erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    late = await _late(world, monkeypatch, scheduled, write, thread)
    assert await _writer(world, late) == _Q

    x_result = await ops.delete_conversation_person(_X)
    paused.go.set()
    q_result = await asyncio.wait_for(q_erase, 30)

    assert (q_result["erased"], x_result["erased"]) == (True, True)
    assert await _records_standing(world, [*ids, late]) == []
    assert world.states_naming(thread) == []


@pytest.mark.parametrize("write", _IN_THE_HOLD)
async def test_a_write_inside_the_drop_is_cleared_with_the_thread(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    write,
):
    thread, ids = await _chat(world, _route_of(write))
    dropping = pause_call(
        monkeypatch, world.lua_env.manager.records, "drop_thread", when=lambda _route, dropped: dropped == thread
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(dropping.reached.wait(), 10)
    late = await _late(world, monkeypatch, scheduled, write, thread)
    dropping.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True

    assert await _records_standing(world, [*ids, late]) == []


async def _a_write_after_the_drop(world: World, monkeypatch, scheduled, point: str, write: str, thread: str) -> str:  # noqa: F811
    """``_Q``'s erase with ``write`` landing in ``thread`` at ``point``, after its drop; answers the
    write's id, standing with the writer ``_Q``."""
    if point == "after_the_drop":
        paused = pause_call(
            monkeypatch,
            world.lua_env.manager.records,
            "drop_thread",
            after=True,
            when=lambda _route, dropped: dropped == thread,
        )
    else:
        paused = pause_call(monkeypatch, world.persons, "erase", when=_erase_of(_Q))
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    late = await _late(world, monkeypatch, scheduled, write, thread)
    paused.go.set()
    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert await _records_standing(world, [late]) == [late]
    assert await _writer(world, late) == _Q
    return late


@pytest.mark.parametrize(("point", "write"), _AFTER_THE_DROP)
async def test_a_write_after_the_drop_goes_with_the_erase_of_the_next_holder_of_the_address(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    point,
    write,
):
    route = _route_of(write)
    thread, _ids = await _chat(world, route)
    late = await _a_write_after_the_drop(world, monkeypatch, scheduled, point, write, thread)
    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=route.target_kind, target_name=route.target_name), channel_address(route, _Q1)
    )
    assert created is True
    assert await _writer(world, late) == _Q
    first = await world.record(f"{minted.person_id}-in-1", route, thread, _Q1)

    assert (await ops.delete_conversation_person(minted.person_id))["erased"] is True

    assert await _records_standing(world, [late, first]) == []


@pytest.mark.parametrize(("point", "write"), _AFTER_THE_DROP)
@pytest.mark.parametrize("linked_into", [False, True])
async def test_a_write_after_the_drop_goes_with_the_next_holder_of_the_address_that_never_writes_there(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    point,
    write,
    linked_into,
):
    route = _route_of(write)
    thread, _ids = await _chat(world, route)
    late = await _a_write_after_the_drop(world, monkeypatch, scheduled, point, write, thread)
    if linked_into:
        await world.seed(person(_Z, channel_address(LINE_B, _Z1), age_days=30))
    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=route.target_kind, target_name=route.target_name), channel_address(route, _Q1)
    )
    assert created is True
    assert thread in await _threads_of(world, minted.person_id)
    erased = minted.person_id
    if linked_into:
        assert (await world.persons.merge(_Z, minted.person_id)).person_id == _Z
        erased = _Z

    assert (await ops.delete_conversation_person(erased))["erased"] is True

    assert await _records_standing(world, [late]) == []


@pytest.mark.parametrize(("point", "write"), _AFTER_THE_DROP)
async def test_a_write_after_the_drop_goes_with_a_thread_delete(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    point,
    write,
):
    route = _route_of(write)
    thread, _ids = await _chat(world, route)
    late = await _a_write_after_the_drop(world, monkeypatch, scheduled, point, write, thread)

    await ops.delete_conversation_thread(route.route_name, thread)

    assert await _records_standing(world, [late]) == []


@pytest.mark.parametrize(("point", "write"), _AFTER_THE_DROP)
async def test_a_write_after_the_drop_goes_with_the_erase_of_an_earlier_writer_of_the_chat(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    point,
    write,
):
    route = _route_of(write)
    thread, _ids = await _chat(world, route, (_X, _I2))
    await ops.delete_conversation_thread(route.route_name, thread)
    await world.record(f"{_Q}-in-again", route, thread, _Q1)
    late = await _a_write_after_the_drop(world, monkeypatch, scheduled, point, write, thread)
    assert thread in await _threads_of(world, _X)

    assert (await ops.delete_conversation_person(_X))["erased"] is True

    assert await _records_standing(world, [late]) == []


async def test_a_write_after_the_final_step_is_its_doors_until_the_next_holder_adopts_it(world):  # noqa: F811
    thread, ids = await _chat(world, LINE)
    assert (await ops.delete_conversation_person(_Q))["erased"] is True
    late = await world.record(f"{_Q}-late", LINE, thread, _Q1)
    assert await _writer(world, late) == await world.redis.hget(world.settings.keyspace.record_key(late), "writer_door")

    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=TARGET.target_kind, target_name=TARGET.target_name), channel_address(LINE, _Q1)
    )

    assert created is True
    assert await _writer(world, late) == minted.person_id
    assert (await ops.delete_conversation_person(minted.person_id))["erased"] is True
    assert await _records_standing(world, [*ids, late]) == []


# -- refusals, stops and retries --------------------------------------------------------------


async def test_a_refusal_under_a_hold_keeps_the_chat_the_other_writers_erase_wiped_meanwhile(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    thread, ids = await _chat(world, LINE, (_X, _I2))
    await world.seed(person(_Q, channel_address(LINE, _Q1), channel_address(LINE, _Z1), age_days=1))
    own = route_thread_id(LINE.route_name, _Z1)
    await world.record(f"{_Q}-own-1", LINE, own, _Z1)
    looks: list[str] = []

    def _the_look_under_the_hold(looked: str, _erased) -> bool:
        # The own chat is looked at by the early check, before its hold, and under its hold.
        looks.extend([looked] if looked == own else [])
        return looked == own and len(looks) == 3

    held = pause_call(monkeypatch, world.persons, "outside_writer", when=_the_look_under_the_hold)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(held.reached.wait(), 10)
    assert (await ops.delete_conversation_person(_X))["erased"] is True
    await intake_module.accept(
        "twilio", LINE_IDENTITY, _Z1, _Z1, "hi", "PID-Z1", medium_message_id="m-PID-Z1", reply_to_medium_message_id=None
    )
    held.go.set()

    with pytest.raises(ConflictError, match=re.escape(own)):
        await asyncio.wait_for(erase, 30)

    assert await _records_standing(world, ids) == []
    assert world.states_naming(thread) == []
    assert await world.redis.exists(world.settings.keyspace.person_key(_Q)) == 1
    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_Q)) == 0
    await _answered(world, scheduled)
    assert (await ops.delete_conversation_person(_Q))["erased"] is True


async def test_a_retry_after_a_stop_leaves_a_chat_another_person_continued_in_between(world, monkeypatch):  # noqa: F811
    thread, ids = await _chat(world, LINE)
    real = threads_delete_module._erase_person_state
    calls: list[str] = []

    async def _failing_once(*args, **kwargs):
        calls.append("state")
        if len(calls) == 1:
            raise RuntimeError("the states store is unreachable")
        return await real(*args, **kwargs)

    monkeypatch.setattr(threads_delete_module, "_erase_person_state", _failing_once)
    with pytest.raises(RuntimeError, match="the states store is unreachable"):
        await ops.delete_conversation_person(_Q)
    assert await _records_standing(world, ids) == []
    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_Q)) == 1
    continued = await _write(world, LINE, _X, _I2, age_days=2)

    assert (await ops.delete_conversation_person(_Q))["erased"] is True

    assert await _records_standing(world, [continued]) == [continued]
    assert thread in await _threads_of(world, _X)


# -- links, unlinks and thread deletes during an erase ----------------------------------------


async def test_a_link_of_the_other_writer_during_the_erase_leaves_the_chat_to_the_survivor(world, monkeypatch):  # noqa: F811
    thread, ids = await _chat(world, LINE, (_X, _I2))
    await world.seed(person(_Y, channel_address(LINE_B, _Y1), age_days=9))
    paused = pause_call(monkeypatch, threads_delete_module, "_clear_route_thread", when=_deciding(_Q, thread))
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    assert (await world.link(_X, _Y)).person_id == _Y
    paused.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert await _records_standing(world, ids) == ids

    assert (await ops.delete_conversation_person(_Y))["erased"] is True
    assert await _records_standing(world, ids) == []


async def test_an_unlink_of_the_other_writers_address_during_the_erase_leaves_the_chat_to_its_old_holder(
    world,  # noqa: F811
    monkeypatch,
):
    thread, ids = await _chat(world, LINE, (_X, _I2))
    await world.seed(person(_X, channel_address(_on(LINE, _I2), _Q1), channel_address(LINE_B, _X2), age_days=2))
    paused = pause_call(monkeypatch, threads_delete_module, "_clear_route_thread", when=_deciding(_Q, thread))
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    fresh = await world.persons.detach(_X, door="channel", channel="twilio", our_identity=_I2, address=_Q1)
    paused.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert await _records_standing(world, ids) == ids
    assert (await ops.delete_conversation_person(fresh.person_id))["erased"] is True
    assert await _records_standing(world, ids) == ids

    assert (await ops.delete_conversation_person(_X))["erased"] is True
    assert await _records_standing(world, ids) == []


@pytest.mark.parametrize("paused_at", ["before_the_hold", "after_the_decision"])
async def test_a_thread_delete_during_the_erase_clears_the_chat_with_it(world, monkeypatch, paused_at):  # noqa: F811
    thread, ids = await _chat(world, LINE)
    paused = pause_call(
        monkeypatch,
        threads_delete_module,
        "_clear_route_thread",
        after=paused_at == "after_the_decision",
        when=_deciding(_Q, thread),
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)

    await ops.delete_conversation_thread(LINE.route_name, thread)
    paused.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert await _records_standing(world, ids) == []
    assert world.states_naming(thread) == []
    assert await _threads_of(world, _Q) == set()


# -- an address nobody holds, and a thread first written during the erase ---------------------


async def test_a_chat_an_address_nobody_holds_wrote_in_is_left_until_a_person_adopts_that_address(
    world,  # noqa: F811
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    thread, ids = route_thread_id(LINE.route_name, _Q1), [await _write(world, LINE, _Q, _I2, age_days=1)]
    sent = await _operator_send(world, monkeypatch, LINE, _Q1)
    assert await _writer(world, sent) == await world.redis.hget(world.settings.keyspace.record_key(sent), "writer_door")

    assert (await ops.delete_conversation_person(_Q))["erased"] is True
    assert await _records_standing(world, [*ids, sent]) == [*ids, sent]

    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=TARGET.target_kind, target_name=TARGET.target_name), channel_address(LINE, _Q1)
    )
    assert created is True
    assert thread in await _threads_of(world, minted.person_id)
    assert (await ops.delete_conversation_person(minted.person_id))["erased"] is True
    assert await _records_standing(world, [*ids, sent]) == []


async def _a_thread_written_after_the_read(world: World, monkeypatch, stale: list[bool]) -> tuple[str, list[str]]:  # noqa: F811
    """``_P``'s api address first writes in ``chat-2`` once the erase's read after the mark is taken.

    While ``stale`` holds ``True``, every later read of the erase is answered that read after the
    mark. Answers the late thread and every record id."""
    await world.routes(LINE, CHAT, CHAT_2)
    await world.seed(person(_P, api_address(_API, CHAT, CHAT_2)))
    ids = [await world.record(f"{_P}-in-1", CHAT, route_thread_id(CHAT.route_name, _API), _API)]
    later = route_thread_id(CHAT_2.route_name, _API)
    real = world.persons.read_for_erase
    reads: list[object] = []

    async def _reads(person_id: str):
        read = await real(person_id)
        reads.append(read)
        if len(reads) == 2:
            ids.append(await world.record(f"{_P}-in-2", CHAT_2, later, _API))
        return reads[1] if stale[0] and len(reads) > 2 else read

    monkeypatch.setattr(world.persons, "read_for_erase", _reads)
    return later, ids


async def test_a_thread_first_written_after_the_read_after_the_mark_is_visited(world, monkeypatch):  # noqa: F811
    later, ids = await _a_thread_written_after_the_read(world, monkeypatch, [False])

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(ids)
    assert await _records_standing(world, ids) == []
    assert later in world.saver.deleted


async def test_a_thread_the_erase_never_visited_stops_its_final_step_and_the_retry_visits_it(
    world,  # noqa: F811
    monkeypatch,
):
    stale = [True]
    later, ids = await _a_thread_written_after_the_read(world, monkeypatch, stale)

    with pytest.raises(ConflictError, match=re.escape(f"in the thread {later!r} landed while the erase of {_P!r} ran")):
        await ops.delete_conversation_person(_P)
    assert await world.redis.exists(world.settings.keyspace.person_key(_P)) == 1
    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_P)) == 1
    assert await _records_standing(world, ids) == ids[1:]
    stale[0] = False

    assert (await ops.delete_conversation_person(_P))["erased"] is True
    assert await _records_standing(world, ids) == []
