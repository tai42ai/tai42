"""A person's erase wipes every route-keyed thread only it and the persons it absorbed wrote in.

Each record written into a route-keyed thread stamps its writer on the thread: the person the
target's index names for the record's door, or the door itself while nobody holds it. The erase
of a person wipes a thread whose writers are all the person or an id it absorbed by a link, and
leaves a thread another writer wrote in, taking the erased ids' stamps off it either way.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission, Person
from tai42_contract.errors import UnavailableError

from tai42_skeleton.conversations.persons import PairingTarget
from tai42_skeleton.conversations.turn import event_door as event_door_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module

from .conftest import _channel_route, _tool_channel_route, pause_call
from .linked_erase import (
    CHAT,
    LINE,
    LINE_B,
    LINE_IDENTITY,
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
from .test_person_erase_absorbed import _operator_send, _park_completes
from .test_person_erase_sends import scheduled  # noqa: F401  # the doors' turn scheduling, held
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture

_P, _Q, _M, _N = "p0000001", "q0000001", "m0000001", "n0000001"
_X, _Y, _Z = "x0000001", "y0000001", "z0000001"
_P1, _Q1, _M1, _Y1 = "+15550001001", "+15550002001", "+15550003001", "+15550004001"
_Z1 = "+15550005001"
_Q_API = "alice/u-7"
_I2 = "+15550003333"
_LINE_I2 = LINE.model_copy(update={"our_identity": _I2})


@pytest.fixture
async def world(lua_env, states_pg, interactions_parks, checkpoint_saver, monkeypatch) -> World:  # noqa: F811
    world = World(lua_env=lua_env, states_pg=states_pg, parks=interactions_parks, saver=checkpoint_saver)
    world.install_channel(monkeypatch)
    return world


async def _line_chat(world: World, person_id: str, address: str, *, age_days: int = 0) -> tuple[str, list[str]]:
    """A person with one address on ``line`` and its chat in ``bridge:line:{address}``.

    An inbound, a ``notify_user`` send, a park on the thread and a ``thread``-subject state.
    Answers the thread and the message ids.
    """
    await world.seed(person(person_id, channel_address(LINE, address), age_days=age_days))
    thread = route_thread_id(LINE.route_name, address)
    ids = [await world.record(f"{person_id}-in-1", LINE, thread, address), await world.notify(address)]
    await world.park(f"{person_id}-park", thread)
    world.thread_state(thread)
    return thread, ids


async def _records_standing(world: World, message_ids: list[str]) -> list[str]:
    return [m for m in message_ids if await world.lua_env.store.get_record(m) is not None]


async def _writers(world: World, thread: str) -> set[str]:
    """The writers of the records standing in ``thread``."""
    return await writers(world.lua_env, thread)


async def _threads_of(world: World, writer: str) -> set[str]:
    """The threads on ``writer``'s visit list."""
    return await world.redis.smembers(world.settings.writer_threads_key(writer))


def _door(identity: str, address: str, target: tuple[str, str] = (TARGET.target_kind, TARGET.target_name)) -> str:
    """The writer a twilio write by ``address`` under ``identity`` stamps while nobody holds its door."""
    dak = json.dumps(["twilio", identity, address], separators=(",", ":"))
    return json.dumps([*target, dak], separators=(",", ":"))


async def _i2_chat(world: World, person_id: str, thread: str, *, age_days: int = 1) -> str:
    """``person_id`` holds the address ``_Q1`` under the identity i2 and writes ``thread``; ``line``
    answers from its first identity again afterwards. Answers the record's id."""
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(person_id, channel_address(_LINE_I2, _Q1), age_days=age_days))
    chat = await world.record(f"{person_id}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    await world.lua_env.manager.put_route(LINE)
    return chat


async def _unlink_q1(world: World, holder: str) -> str:
    """``_Q1`` on ``line`` unlinked from ``holder`` into a fresh person; answers its id."""
    detached = await world.persons.detach(
        holder, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1
    )
    return detached.person_id


async def test_an_erase_wipes_every_route_thread_its_person_and_its_absorbed_persons_wrote(world):
    await world.routes(LINE, LINE_B, CHAT)
    p_thread, p_ids = await _line_chat(world, _P, _P1, age_days=1)
    await world.seed(person(_Q, channel_address(LINE_B, _Q1), api_address(_Q_API, CHAT)))
    q_line = route_thread_id(LINE_B.route_name, _Q1)
    q_api = route_thread_id(CHAT.route_name, _Q_API)
    q_ids = [
        await world.record(f"{_Q}-in-1", LINE_B, q_line, _Q1),
        await world.record(f"{_Q}-in-2", CHAT, q_api, _Q_API),
    ]
    world.thread_state(q_line)
    world.thread_state(q_api)
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + len(q_ids)
    assert result["absorbed_person_ids"] == [_Q]
    await assert_clean(
        world,
        _P,
        _Q,
        _P1,
        _Q1,
        _Q_API,
        *p_ids,
        *q_ids,
        threads=(person_thread_id(_P), p_thread, q_line, q_api),
        parks=(f"{_P}-park",),
    )


async def test_the_survivors_own_chat_from_before_its_link_is_wiped_by_its_erase(world):
    await world.routes(LINE, LINE_B)
    x_thread, x_ids = await _line_chat(world, _X, _Q1, age_days=3)
    await world.seed(person(_Y, channel_address(LINE_B, _Y1), age_days=1))
    y_thread = route_thread_id(LINE_B.route_name, _Y1)
    y_ids = [await world.record(f"{_Y}-in-1", LINE_B, y_thread, _Y1)]
    assert (await world.link(_Y, _X)).person_id == _X

    result = await ops.delete_conversation_person(_X)

    assert result["removed"] == len(x_ids) + len(y_ids)
    await assert_clean(world, _X, _Y, _Q1, _Y1, *x_ids, *y_ids, threads=(x_thread, y_thread), parks=(f"{_X}-park",))


async def test_a_thread_left_by_one_erase_is_wiped_by_the_erase_of_the_person_who_continued_it(world):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=9))
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    await world.seed(person(_M, channel_address(LINE_B, _M1), age_days=6))
    assert (await world.link(_Q, _M)).person_id == _M
    detached = await world.persons.detach(_M, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1)
    n_ids = [await world.record("n-in-1", LINE, thread, _Q1)]

    first = await ops.delete_conversation_person(_M)

    assert first["erased"] is True
    assert await _records_standing(world, [*q_ids, *n_ids]) == [*q_ids, *n_ids]
    assert (await world.link(detached.person_id, _P)).person_id == _P

    second = await ops.delete_conversation_person(_P)

    assert second["removed"] == len(q_ids) + len(n_ids)
    await assert_clean(
        world, _P, _Q, _M, detached.person_id, _Q1, *q_ids, *n_ids, threads=(thread,), parks=(f"{_Q}-park",)
    )


async def test_an_address_unlinked_and_linked_back_without_a_write_is_erased_with_its_holder(world):
    await world.routes(LINE, LINE_B)
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    await world.seed(person(_M, channel_address(LINE_B, _M1), age_days=6))
    assert (await world.link(_Q, _M)).person_id == _M
    n_id = await _unlink_q1(world, _M)
    assert (await world.link(n_id, _M)).person_id == _M
    assert await _writers(world, thread) == {_Q}

    result = await ops.delete_conversation_person(_M)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == sorted([_Q, n_id])
    await assert_clean(world, _M, _Q, n_id, _Q1, *q_ids, threads=(thread,), parks=(f"{_Q}-park",))


async def test_a_thread_delete_clears_its_writers_and_the_next_write_starts_them_again(world):
    await world.routes(LINE, LINE_B)
    thread, _q_ids = await _line_chat(world, _Q, _Q1)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.record("i2-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    await world.lua_env.manager.put_route(LINE)
    assert await _writers(world, thread) == {_Q, _door(_I2, _Q1)}

    await ops.delete_conversation_thread(LINE.route_name, thread)

    assert await _writers(world, thread) == set()
    assert await _threads_of(world, _Q) == {thread}
    assert await _threads_of(world, _door(_I2, _Q1)) == {thread}
    again = await world.record(f"{_Q}-in-2", LINE, thread, _Q1)
    assert await _writers(world, thread) == {_Q}
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=5))
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == 1
    door = world.settings.writer_threads_key(_door(_I2, _Q1))
    assert await leftovers(world, _P, _Q, again, threads=(thread,)) == {"keys": {door: [thread]}}


async def test_a_thread_another_person_wrote_in_before_the_link_is_left_until_that_person_is_absorbed(world):
    await world.routes(LINE, LINE_B)
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    x_ids = [await _i2_chat(world, _X, thread)]
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=5))
    await world.seed(person(_M, channel_address(LINE_B, _M1), age_days=6))
    assert (await world.link(_Q, _P)).person_id == _P

    first = await ops.delete_conversation_person(_P)

    assert first["erased"] is True
    assert await _records_standing(world, [*q_ids, *x_ids]) == [*q_ids, *x_ids]
    assert world.states_naming(thread) != []
    assert await _writers(world, thread) == {_Q, _X}
    assert (await world.link(_X, _M)).person_id == _M

    second = await ops.delete_conversation_person(_M)

    assert second["removed"] == len(q_ids) + len(x_ids)
    await assert_clean(world, _M, _X, _Q, *q_ids, *x_ids, threads=(thread,), parks=(f"{_Q}-park",))


async def _shared_chat(world: World) -> tuple[str, list[str]]:
    await world.routes(LINE, LINE_B)
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    x_ids = [await _i2_chat(world, _X, thread)]
    assert await _writers(world, thread) == {_Q, _X}
    return thread, [*q_ids, *x_ids]


async def _assert_shared_chat_wiped(world: World, thread: str, ids: list[str]) -> None:
    assert await _records_standing(world, ids) == []
    assert await _writers(world, thread) == set()
    assert world.states_naming(thread) == []
    assert thread in world.saver.deleted


async def test_two_erases_in_a_row_of_both_writers_wipe_their_shared_chat(world):
    thread, ids = await _shared_chat(world)

    assert (await ops.delete_conversation_person(_Q))["erased"] is True
    assert (await ops.delete_conversation_person(_X))["erased"] is True

    await _assert_shared_chat_wiped(world, thread, ids)


async def _late_write_goes_with_the_next_holders_chat(world: World, thread: str, late: str, *, writes: bool) -> None:
    """``late`` stands in ``thread`` under its erased writer ``_Q``; the person minted for the door
    writes there or not (``writes``), and that person's erase wipes the thread."""
    assert await world.redis.hget(world.settings.record_key(late), "writer") == _Q
    assert await _writers(world, thread) == {_Q}
    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=TARGET.target_kind, target_name=TARGET.target_name), channel_address(LINE, _Q1)
    )
    assert created is True
    assert await _threads_of(world, minted.person_id) == {thread}
    again = [await world.record(f"{minted.person_id}-in-1", LINE, thread, _Q1)] if writes else []

    assert (await ops.delete_conversation_person(minted.person_id))["erased"] is True

    assert await _records_standing(world, [late, *again]) == []
    assert await _writers(world, thread) == set()


@pytest.mark.parametrize("writes", [True, False])
@pytest.mark.parametrize("cleared_by", ["the_other_writers_erase", "a_thread_delete"])
async def test_a_write_in_a_left_chat_cleared_during_the_erase_goes_with_the_next_holders_chat(
    world, monkeypatch, cleared_by, writes
):
    thread, ids = await _shared_chat(world)
    paused = pause_call(monkeypatch, world.persons, "erase", when=lambda erased, *_a, **_k: erased.person_id == _Q)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    assert await _writers(world, thread) == {_Q, _X}
    if cleared_by == "the_other_writers_erase":
        assert (await ops.delete_conversation_person(_X))["erased"] is True
    else:
        await ops.delete_conversation_thread(LINE.route_name, thread)
    assert await _records_standing(world, ids) == []
    late = await world.record(f"{_Q}-late", LINE, thread, _Q1)
    paused.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True

    await _late_write_goes_with_the_next_holders_chat(world, thread, late, writes=writes)


async def test_a_write_in_a_left_chat_the_other_writer_still_holds_keeps_its_writer(world, monkeypatch):
    thread, ids = await _shared_chat(world)
    paused = pause_call(monkeypatch, world.persons, "erase", when=lambda erased, *_a, **_k: erased.person_id == _Q)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    late = await world.record(f"{_Q}-late", LINE, thread, _Q1)
    paused.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True

    assert await _records_standing(world, [*ids, late]) == [*ids, late]
    assert await world.redis.hget(world.settings.record_key(late), "writer") == _Q
    assert await _writers(world, thread) == {_Q, _X}
    assert (await ops.delete_conversation_person(_X))["erased"] is True
    await _assert_shared_chat_wiped(world, thread, [*ids, late])


async def test_a_chat_the_other_writers_erase_cleared_during_a_refused_erase_stays_cleared(world, monkeypatch):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_Q, channel_address(LINE, _Q1), channel_address(LINE, _Z1)))
    shared = route_thread_id(LINE.route_name, _Q1)
    own = route_thread_id(LINE.route_name, _Z1)
    await world.record(f"{_Q}-in-2", LINE, own, _Z1)
    ids = [await world.record(f"{_Q}-in-1", LINE, shared, _Q1), await _i2_chat(world, _X, shared)]
    world.thread_state(shared)
    paused = pause_call(monkeypatch, threads_delete_module, "_clear_route_thread", when=lambda _p, _e, t, _c: t == own)
    real = threads_delete_module._erase_thread_state

    async def _unsettled_on_the_own_chat(route_names: list[str], thread_id: str, also_targets=()) -> int:
        if thread_id == own:
            raise UnavailableError("the pending saves are unsettled")
        return await real(route_names, thread_id, also_targets)

    monkeypatch.setattr(threads_delete_module, "_erase_thread_state", _unsettled_on_the_own_chat)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    assert (await ops.delete_conversation_person(_X))["erased"] is True
    paused.go.set()

    with pytest.raises(UnavailableError):
        await asyncio.wait_for(erase, 30)

    assert await world.redis.exists(world.settings.person_erasing_key(_Q)) == 0
    assert await world.redis.exists(world.settings.person_key(_Q)) == 1
    await _assert_shared_chat_wiped(world, shared, ids)


async def test_an_erase_refused_after_leaving_a_shared_chat_keeps_its_absorbed_ids_writes_there(world, monkeypatch):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE, _Z1), age_days=2))
    await world.seed(person(_Q, channel_address(LINE, _Q1)))
    own = route_thread_id(LINE.route_name, _Z1)
    shared = route_thread_id(LINE.route_name, _Q1)
    await world.record(f"{_P}-in-1", LINE, own, _Z1)
    ids = [await world.record(f"{_Q}-in-1", LINE, shared, _Q1), await _i2_chat(world, _X, shared)]
    world.thread_state(shared)
    assert (await world.link(_Q, _P)).person_id == _P
    assert await _writers(world, shared) == {_Q, _X}
    real = threads_delete_module._erase_thread_state

    async def _unsettled_on_the_own_chat(route_names: list[str], thread_id: str, also_targets=()) -> int:
        if thread_id == own:
            raise UnavailableError("the pending saves are unsettled")
        return await real(route_names, thread_id, also_targets)

    monkeypatch.setattr(threads_delete_module, "_erase_thread_state", _unsettled_on_the_own_chat)
    with pytest.raises(UnavailableError):
        await ops.delete_conversation_person(_P)
    monkeypatch.setattr(threads_delete_module, "_erase_thread_state", real)

    assert await _writers(world, shared) == {_Q, _X}
    assert (await ops.delete_conversation_person(_X))["erased"] is True
    assert await _records_standing(world, ids) == ids
    assert (await ops.delete_conversation_person(_P))["erased"] is True
    await _assert_shared_chat_wiped(world, shared, ids)


async def test_an_erase_refused_after_leaving_a_shared_chat_keeps_its_persons_writes_there(world, monkeypatch):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_Q, channel_address(LINE, _Q1), channel_address(LINE, _Z1)))
    shared = route_thread_id(LINE.route_name, _Q1)
    own = route_thread_id(LINE.route_name, _Z1)
    await world.record(f"{_Q}-in-2", LINE, own, _Z1)
    ids = [await world.record(f"{_Q}-in-1", LINE, shared, _Q1), await _i2_chat(world, _X, shared)]
    world.thread_state(shared)
    real = threads_delete_module._erase_thread_state

    async def _unsettled_on_the_own_chat(route_names: list[str], thread_id: str, also_targets=()) -> int:
        if thread_id == own:
            raise UnavailableError("the pending saves are unsettled")
        return await real(route_names, thread_id, also_targets)

    monkeypatch.setattr(threads_delete_module, "_erase_thread_state", _unsettled_on_the_own_chat)
    with pytest.raises(UnavailableError):
        await ops.delete_conversation_person(_Q)
    monkeypatch.setattr(threads_delete_module, "_erase_thread_state", real)

    assert await _writers(world, shared) == {_Q, _X}
    assert shared in await _threads_of(world, _Q)
    assert (await ops.delete_conversation_person(_X))["erased"] is True
    assert await _records_standing(world, ids) == ids
    assert await _writers(world, shared) == {_Q, _X}

    assert (await ops.delete_conversation_person(_Q))["erased"] is True
    await _assert_shared_chat_wiped(world, shared, ids)


async def test_an_erase_stopped_after_leaving_a_shared_chat_is_finished_by_its_retry(world, monkeypatch):
    thread, ids = await _shared_chat(world)
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
    assert await world.redis.exists(world.settings.person_erasing_key(_Q)) == 1
    assert await _writers(world, thread) == {_Q, _X}

    assert (await ops.delete_conversation_person(_Q))["erased"] is True
    assert await _records_standing(world, ids) == ids
    assert await _writers(world, thread) == {_Q, _X}
    assert (await ops.delete_conversation_person(_X))["erased"] is True
    await _assert_shared_chat_wiped(world, thread, ids)


async def test_a_foreign_write_between_the_check_and_the_hold_leaves_the_thread(world, monkeypatch):
    await world.routes(LINE, LINE_B)
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    await world.seed(person(_X, channel_address(_LINE_I2, _Q1)), state=False)
    checks: list[str] = []

    def _the_check_before_the_hold(checked: str) -> bool:
        if checked == thread:
            checks.append(checked)
        return checked == thread and len(checks) == 2

    paused = pause_call(
        monkeypatch,
        world.lua_env.manager.records,
        "thread_has_live_intake",
        after=True,
        when=_the_check_before_the_hold,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(paused.reached.wait(), 10)
    await world.lua_env.manager.put_route(_LINE_I2)
    x_chat = await world.record(f"{_X}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    paused.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert result["erased"] is True
    assert await _records_standing(world, [*q_ids, x_chat]) == [*q_ids, x_chat]
    assert await _writers(world, thread) == {_Q, _X}


@pytest.mark.parametrize("lands", ["before_the_check", "inside_the_drop"])
async def test_a_foreign_write_before_the_check_leaves_the_thread_and_inside_the_drop_goes_with_it(
    world, monkeypatch, lands
):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=5))
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    await world.seed(person(_X, channel_address(_LINE_I2, _Q1)), state=False)
    if lands == "before_the_check":
        paused = pause_call(
            monkeypatch, threads_delete_module, "_clear_route_thread", when=lambda _p, _e, t, _c: t == thread
        )
    else:
        paused = pause_call(monkeypatch, world.lua_env.manager.records, "drop_thread", when=lambda _r, t: t == thread)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(paused.reached.wait(), 10)
    await world.lua_env.manager.put_route(_LINE_I2)
    x_chat = await world.record(f"{_X}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    paused.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert result["erased"] is True
    if lands == "before_the_check":
        assert await _records_standing(world, [*q_ids, x_chat]) == [*q_ids, x_chat]
        assert world.states_naming(thread) != []
        assert await _writers(world, thread) == {_Q, _X}
        return
    assert await _records_standing(world, [*q_ids, x_chat]) == []
    assert world.states_naming(thread) == []
    assert await _writers(world, thread) == set()
    assert await _threads_of(world, _X) == {thread}


async def test_a_persons_first_message_is_its_own_write(world, scheduled):  # noqa: F811
    await world.routes(LINE)
    thread = route_thread_id(LINE.route_name, _Q1)
    relay = _channel_route("relay", LINE_IDENTITY)
    relay_thread = route_thread_id(relay.route_name, _Q1)
    await world.record("relay-in-1", relay, relay_thread, _Q1)
    first = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-A1", medium_message_id="m-PID-A1", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    assert (await world.lua_env.store.get_record(first)).thread_id == thread
    door = _door(LINE_IDENTITY, _Q1)
    assert await _threads_of(world, door) == {thread, relay_thread}

    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=TARGET.target_kind, target_name=TARGET.target_name), channel_address(LINE, _Q1)
    )

    assert created is True
    assert await _writers(world, thread) == {minted.person_id}
    assert await _threads_of(world, minted.person_id) == {thread, relay_thread}
    assert await _threads_of(world, door) == set()


async def test_a_write_after_the_drop_by_the_person_being_erased_goes_with_the_next_holders_chat(world, monkeypatch):
    await world.routes(LINE)
    thread, p_ids = await _line_chat(world, _P, _P1)
    dropped = pause_call(
        monkeypatch,
        world.lua_env.manager.records,
        "drop_thread",
        after=True,
        when=lambda _r, t: t == thread,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(dropped.reached.wait(), 10)
    late = await world.record(f"{_P}-late", LINE, thread, _P1)
    dropped.go.set()
    await asyncio.wait_for(erase, 30)

    assert await _records_standing(world, [*p_ids, late]) == [late]
    assert await _writers(world, thread) == {_P}
    assert await _threads_of(world, _P) == set()

    minted, created = await world.persons.ensure_provisional(
        PairingTarget(target_kind=TARGET.target_kind, target_name=TARGET.target_name), channel_address(LINE, _P1)
    )
    assert created is True
    again = await world.record(f"{minted.person_id}-in-1", LINE, thread, _P1)
    assert await _writers(world, thread) == {_P, minted.person_id}

    assert (await ops.delete_conversation_person(minted.person_id))["erased"] is True

    assert await _records_standing(world, [late, again]) == []


_EVENT_IDENTITY = "+15550007777"
_EVENT_ROUTE = _tool_channel_route(our_identity=_EVENT_IDENTITY)
_EVENT_TARGET = (_EVENT_ROUTE.target_kind, _EVENT_ROUTE.target_name)


async def _event_writes(world: World, thread: str) -> tuple[str, str]:
    """An event on the tool route's thread of ``_Q1``, after ``_Z`` came to hold the address on the
    tool target. Answers the door's stamp and Z's id."""
    await world.lua_env.manager.put_route(_EVENT_ROUTE)
    await world.record("event-line-in-1", _EVENT_ROUTE, thread, _Q1)
    await world.lua_env.seed_person(
        Person(
            person_id=_Z,
            target_kind=_EVENT_TARGET[0],
            target_name=_EVENT_TARGET[1],
            created_at=datetime.now(UTC),
            addresses=[channel_address(_EVENT_ROUTE, _Q1)],
        )
    )

    async def _connected() -> bool:
        return True

    await event_door_module.submit_event(
        _EVENT_ROUTE.route_name,
        ConversationEventSubmission(
            thread_id=thread, event=ConversationEvent(event_id="evt-1", kind="provider.update", payload={})
        ),
        "svc-int",
        client_connected=_connected,
    )
    return _door(_EVENT_IDENTITY, _Q1, _EVENT_TARGET), _Z


@pytest.mark.parametrize(
    "door",
    [
        "inbound_create",
        "park_completion",
        "operator_send",
        "send_begin",
        "completion_landing",
        "end_landing",
        "event",
    ],
)
async def test_every_record_write_into_a_route_thread_stamps_its_writer(
    world,
    monkeypatch,
    scheduled,  # noqa: F811
    door,
):
    await world.routes(LINE)
    if door == "event":
        thread = route_thread_id(_EVENT_ROUTE.route_name, _Q1)
        unheld, writer = await _event_writes(world, thread)
        assert [intake.thread_id for intake in scheduled] == [thread]
        assert await _writers(world, thread) == {unheld, writer}
        assert await _threads_of(world, writer) == {thread}
        return
    thread = route_thread_id(LINE.route_name, _Q1)
    unheld = _door(LINE_IDENTITY, _Q1)
    landing = await world.held_send(_Q1) if door in ("completion_landing", "end_landing") else None
    if landing is None:
        await world.record("unheld-in-1", LINE, thread, _Q1)
    assert await _threads_of(world, unheld) == {thread}
    await world.seed(person(_Q, channel_address(LINE, _Q1)), state=False)

    begun: HeldSend | None = None
    if landing is not None and door == "completion_landing":
        await landing.answer(_Q1, LINE_IDENTITY)
    elif landing is not None:
        await landing.fail()
    elif door == "inbound_create":
        await intake_module.accept(
            "twilio",
            LINE_IDENTITY,
            _Q1,
            _Q1,
            "hi",
            "PID-S1",
            medium_message_id="m-PID-S1",
            reply_to_medium_message_id=None,
        )
        assert [intake.thread_id for intake in scheduled] == [thread]
    elif door == "park_completion":
        await world.park(f"{_Q}-park", thread)
        await _park_completes(world, monkeypatch, thread, f"{_Q}-done")
    elif door == "operator_send":
        await _operator_send(world, monkeypatch, LINE, _Q1)
    else:
        begun = sending = await world.held_send(_Q1)
        begun_record = await world.lua_env.store.get_record(sending.message_id)
        assert begun_record is not None
        assert begun_record.thread_id == thread

    if begun is None:
        assert _Q in await _writers(world, thread)
    else:
        assert await world.redis.hget(world.settings.record_key(begun.message_id), "writer") == _Q
    assert await _threads_of(world, _Q) == {thread}
    if begun is not None:
        await begun.fail()


@pytest.mark.parametrize("n_into", ["nobody", "another_survivor"])
async def test_a_fresh_persons_write_after_a_thread_delete_leaves_the_thread_to_it(world, n_into):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=5))
    thread, _q_ids = await _line_chat(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    await ops.delete_conversation_thread(LINE.route_name, thread)
    n_id = await _unlink_q1(world, _P)
    n_chat = await world.record("n-in-1", LINE, thread, _Q1)
    world.thread_state(thread)
    if n_into == "another_survivor":
        await world.seed(person(_M, channel_address(LINE_B, _M1), age_days=4))
        assert (await world.link(n_id, _M)).person_id == _M
    x_chat = await _i2_chat(world, _X, thread)
    assert (await world.link(_X, _P)).person_id == _P
    assert await _writers(world, thread) == {n_id, _X}

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == sorted([_Q, _X])
    assert await _records_standing(world, [n_chat, x_chat]) == [n_chat, x_chat]
    assert world.states_naming(thread) != []
    assert await _writers(world, thread) == {n_id, _X}


async def test_a_thread_two_persons_wrote_before_a_chain_of_links_is_wiped_by_one_erase(world):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=5))
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    x_ids = [await _i2_chat(world, _X, thread)]
    await world.seed(person(_M, channel_address(LINE_B, _M1), age_days=4))
    assert (await world.link(_X, _M)).person_id == _M
    assert (await world.link(_Q, _P)).person_id == _P
    assert (await world.link(_M, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == sorted([_M, _Q, _X])
    assert result["removed"] == len(q_ids) + len(x_ids)
    await assert_clean(world, _P, _Q, _X, _M, *q_ids, *x_ids, threads=(thread,), parks=(f"{_Q}-park",))


@pytest.mark.parametrize("fresh_writes", [False, True])
@pytest.mark.parametrize("linked", ["directly", "by_a_chain"])
async def test_a_survivors_chat_stands_when_its_unlinked_address_is_erased_by_another_person(
    world, fresh_writes, linked
):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=9))
    thread, x_ids = await _line_chat(world, _X, _Q1, age_days=3)
    await world.seed(person(_Y, channel_address(LINE_B, _Y1), age_days=1))
    assert (await world.link(_Y, _X)).person_id == _X
    n_id = await _unlink_q1(world, _X)
    n_ids = [await world.record("n-in-1", LINE, thread, _Q1)] if fresh_writes else []
    if linked == "by_a_chain":
        await world.seed(person(_M, channel_address(LINE_B, _M1), age_days=6))
        assert (await world.link(n_id, _M)).person_id == _M
        assert (await world.link(_M, _P)).person_id == _P
    else:
        assert (await world.link(n_id, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert await world.persons.get_by_id(_X) is not None
    assert await _records_standing(world, [*x_ids, *n_ids]) == [*x_ids, *n_ids]
    assert world.states_naming(thread) != []
    assert await _writers(world, thread) == ({_X, n_id} if fresh_writes else {_X})


async def test_an_erase_stopped_by_a_drop_and_then_linked_into_an_older_person_is_finished_by_its_erase(
    world, monkeypatch
):
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _P1), age_days=5))
    thread, q_ids = await _line_chat(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    records = world.lua_env.manager.records
    real_drop = records.drop_thread
    drops: list[str] = []

    async def _drop_fails_once(route_name: str, thread_id: str) -> int:
        if thread_id == thread and not drops:
            drops.append(thread_id)
            raise RuntimeError("the records store is unreachable")
        return await real_drop(route_name, thread_id)

    monkeypatch.setattr(records, "drop_thread", _drop_fails_once)
    with pytest.raises(RuntimeError, match="the records store is unreachable"):
        await ops.delete_conversation_person(_P)
    await world.seed(person(_Z, channel_address(LINE_B, _Z1), age_days=9))
    assert (await world.link(_P, _Z)).person_id == _Z

    result = await ops.delete_conversation_person(_Z)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_P, _Q]
    await assert_clean(world, _Z, _P, _Q, *q_ids, threads=(thread,), parks=(f"{_Q}-park",))
