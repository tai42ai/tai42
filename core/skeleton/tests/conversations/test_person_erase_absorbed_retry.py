"""A person erase over an absorbed person's route threads across a failed attempt and its retry.

The writer stamps an interrupted clearing leaves for its retry, writes that land around the
clearing, the turns in flight it refuses or leaves, and a chain of links; the persons run the real
script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from tai42_contract.errors import UnavailableError

from tai42_skeleton.conversations.caps import get_turn_caps
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.thread_ids import route_thread_id
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module
from tai42_skeleton.operations.errors import ConflictError

from .conftest import _channel_route, pause_call
from .linked_erase import LINE, LINE_B, LINE_B_IDENTITY, LINE_IDENTITY, channel_address, leftovers, person
from .test_person_erase_absorbed import (
    _I2,
    _LINE_I2,
    _M,
    _M1,
    _M2,
    _P,
    _P1,
    _P2,
    _Q,
    _Q1,
    _Q2,
    _R,
    _R1,
    _answered,
    _operator_send,
    _park_completes,
    _q_chat_on_line,
    _single_address_person,
    _threads_of,
    _two_address_person,
    _writers,
    world,  # noqa: F401  # the linked-erase world fixture
)
from .test_person_erase_sends import scheduled  # noqa: F401  # the doors' turn scheduling, held
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture


def _state_clear_fails_once(monkeypatch) -> list[str]:
    """The thread state clear raises the retriable error on its first call; answers the calls."""
    real = threads_delete_module._erase_thread_state
    calls: list[str] = []

    async def _failing_once(route_names: list[str], thread_id: str, also_targets=()) -> int:
        calls.append(thread_id)
        if len(calls) == 1:
            raise UnavailableError("the pending saves are unsettled")
        return await real(route_names, thread_id, also_targets)

    monkeypatch.setattr(threads_delete_module, "_erase_thread_state", _failing_once)
    return calls


_STEPS = ("cancel_parks", "checkpoint", "drop_thread", "state_clear")


def _each_step_fails_once(monkeypatch, world, thread: str) -> list[str]:  # noqa: F811
    """Each clearing step of ``thread`` raises on one attempt, in turn; answers the steps that failed."""
    from tai42_skeleton.interactions import helper as interactions_helper

    failed: list[str] = []
    manager = world.lua_env.manager

    def _failing(step: str, owner, name: str, error: Exception, matches) -> None:
        real = getattr(owner, name)

        async def _call(*args, **kwargs):
            if matches(*args, **kwargs) and step not in failed and len(failed) == _STEPS.index(step):
                failed.append(step)
                raise error
            return await real(*args, **kwargs)

        monkeypatch.setattr(owner, name, _call)

    _failing(
        "cancel_parks",
        interactions_helper,
        "cancel_parks_for_thread",
        RuntimeError("the parks store is unreachable"),
        lambda thread_id, **_: thread_id == thread,
    )
    _failing(
        "checkpoint",
        threads_delete_module,
        "_delete_thread_checkpoint",
        RuntimeError("the checkpoint store is unreachable"),
        lambda thread_id: thread_id == thread,
    )
    _failing(
        "drop_thread",
        manager.records,
        "drop_thread",
        RuntimeError("the records store is unreachable"),
        lambda route_name, thread_id: thread_id == thread,
    )
    _failing(
        "state_clear",
        threads_delete_module,
        "_erase_thread_state",
        UnavailableError("the pending saves are unsettled"),
        lambda route_names, thread_id, also_targets=(): thread_id == thread,
    )
    return failed


async def test_an_erase_stopped_after_a_thread_drop_is_finished_by_its_retry(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    await world.park(f"{_Q}-park", thread)
    world.thread_state(thread)
    assert (await world.link(_Q, _P)).person_id == _P
    failed = _each_step_fails_once(monkeypatch, world, thread)

    for step in _STEPS:
        with pytest.raises((RuntimeError, UnavailableError)):
            await ops.delete_conversation_person(_P)
        assert failed[-1] == step
        assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_P)) == 0
        assert thread in await _threads_of(world, _Q)
        assert thread in (await world.persons.read_for_erase(_P)).route_threads

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_Q]
    assert result["removed"] == len(p_ids)
    assert await leftovers(world, _P, _Q, chat, threads=(thread,), parks=(f"{_Q}-park",)) == {}


async def test_a_write_inside_the_clearing_is_cleared_with_the_thread(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    world.thread_state(thread)
    assert (await world.link(_Q, _P)).person_id == _P
    await world.seed(person(_M, channel_address(_LINE_I2, _Q1)), state=False)
    dropping = pause_call(
        monkeypatch,
        world.lua_env.manager.records,
        "drop_thread",
        when=lambda route_name, thread_id: thread_id == thread,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(dropping.reached.wait(), 10)
    await world.lua_env.manager.put_route(_LINE_I2)
    m_write = await world.record(f"{_M}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    assert await _writers(world, thread) == {_Q, _M}
    dropping.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 2
    for message_id in (chat, m_write):
        assert await world.lua_env.store.get_record(message_id) is None
    assert thread in world.saver.deleted
    assert world.states_naming(thread) == []
    assert await _writers(world, thread) == set()
    assert await _threads_of(world, _M) == {thread}


@pytest.mark.parametrize("continued_by", ["an_unlink", "an_unlink_then_a_link", "none"])
async def test_a_thread_continued_between_a_failed_attempt_and_its_retry_is_left(
    world,  # noqa: F811
    monkeypatch,
    continued_by,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    m_ids = await _two_address_person(world, _M, _M1, _M2, age_days=2)
    thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    _state_clear_fails_once(monkeypatch)
    with pytest.raises(UnavailableError):
        await ops.delete_conversation_person(_P)
    assert (await _writers(world, thread), await _threads_of(world, _Q)) == (set(), {thread})
    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_P)) == 0
    forgotten_before = len(world.saver.deleted)

    if continued_by == "none":
        p_send = await _operator_send(world, monkeypatch, LINE, _Q1)
        assert await _writers(world, thread) == {_P}

        result = await ops.delete_conversation_person(_P)

        assert result["erased"] is True
        assert result["removed"] == len(p_ids) + 1
        assert await leftovers(world, _P, _Q, p_send, *q_ids, threads=(thread,), parks=(f"{_Q}-park-thread",)) == {}
        return
    detached = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1)
    n_id = detached.person_id
    n_write = await world.record(f"{n_id}-in-1", LINE, thread, _Q1)
    assert await _writers(world, thread) == {n_id}
    if continued_by == "an_unlink_then_a_link":
        assert (await world.link(n_id, _M)).person_id == _M

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids)
    assert (await world.lua_env.store.get_record(n_write)).thread_id == thread
    assert thread not in world.saver.deleted[forgotten_before:]
    assert await _writers(world, thread) == {n_id}
    if continued_by == "an_unlink":
        assert (await world.index())[world.index_field(channel_address(LINE, _Q1))] == n_id
        return

    result = await ops.delete_conversation_person(_M)

    assert result["erased"] is True
    assert result["removed"] == len(m_ids) + 1
    assert await world.lua_env.store.get_record(n_write) is None
    assert thread in world.saver.deleted[forgotten_before:]
    assert await leftovers(world, _M, n_id, n_write, threads=(thread,)) == {}


_RELAY = _channel_route("relay", LINE_IDENTITY)


async def _q_on_two_routes(world) -> tuple[list[str], str, list[str], str, list[str]]:  # noqa: F811
    """Q's one address chats in ``bridge:line:{a}``; ``line`` moves to the identity i2, and Q's address
    then writes ``bridge:relay:{a}`` on a new route under the first identity. Answers P's ids, the
    first thread with Q's ids in it, and the relay thread with its ids."""
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    first, q_ids = await _single_address_person(world, _Q, _Q1)
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.lua_env.manager.put_route(_RELAY)
    relay = route_thread_id(_RELAY.route_name, _Q1)
    relay_ids = [await world.record(f"{_Q}-in-r", _RELAY, relay, _Q1)]
    await world.park(f"{_Q}-park-r", relay)
    world.thread_state(relay)
    return p_ids, first, q_ids, relay, relay_ids


async def test_a_refusal_after_one_route_thread_was_cleared_keeps_that_clearing(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    _p_ids, first, q_ids, relay, relay_ids = await _q_on_two_routes(world)
    assert (await world.link(_Q, _P)).person_id == _P
    assert (await world.persons.read_for_erase(_P)).route_threads == [first, relay]
    reached = pause_call(
        monkeypatch,
        threads_delete_module,
        "_clear_route_thread",
        when=lambda _person, _erased, thread, _caps: thread == relay,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(reached.reached.wait(), 10)
    assert world.states_naming(first) == []
    await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-W2", medium_message_id="m-PID-W2", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [relay]
    reached.go.set()

    with pytest.raises(ConflictError):
        await asyncio.wait_for(erase, 30)

    assert world.states_naming(first) == []
    assert await _writers(world, first) == set()
    assert first in await _threads_of(world, _Q)
    assert world.states_naming(relay) != []
    assert await _writers(world, relay) == {_Q, _P}
    await _answered(world, scheduled)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert world.states_naming(relay) == []
    assert await leftovers(world, _P, _Q, *q_ids, *relay_ids, threads=(first, relay)) == {}


async def test_a_turns_state_in_a_thread_written_after_its_clearing_is_kept(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, q_ids = await _single_address_person(world, _Q, _Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    final = pause_call(monkeypatch, threads_delete_module, "_erase_person_state")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(final.reached.wait(), 10)
    assert world.states_naming(thread) == []
    assert await _writers(world, thread) == set()
    await world.lua_env.manager.put_route(_LINE_I2)
    await world.seed(person(_M, channel_address(_LINE_I2, _Q1)), state=False)
    m_write = await world.record(f"{_M}-in-1", _LINE_I2, thread, _Q1, our_identity=_I2)
    world.thread_state(thread)
    final.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + len(q_ids)
    assert world.states_naming(thread) != []
    assert (await world.lua_env.store.get_record(m_write)).thread_id == thread
    assert await _writers(world, thread) == {_M}


async def test_a_thread_delete_stopped_by_its_drop_is_finished_by_its_retry(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    thread, q_ids = await _single_address_person(world, _Q, _Q1)
    records = world.lua_env.manager.records
    real_drop = records.drop_thread
    drops: list[str] = []

    async def _drop_fails_once(route_name: str, thread_id: str) -> int:
        drops.append(thread_id)
        if len(drops) == 1:
            raise RuntimeError("the records store is unreachable")
        return await real_drop(route_name, thread_id)

    monkeypatch.setattr(records, "drop_thread", _drop_fails_once)

    with pytest.raises(RuntimeError, match="the records store is unreachable"):
        await ops.delete_conversation_thread(LINE.route_name, thread)

    for message_id in q_ids:
        assert (await world.lua_env.store.get_record(message_id)).thread_id == thread
    assert await _writers(world, thread) == {_Q}
    between = await world.record(f"{_Q}-in-2", LINE, thread, _Q1)

    result = await ops.delete_conversation_thread(LINE.route_name, thread)

    assert result["removed"] == len(q_ids) + 1
    assert await _writers(world, thread) == set()
    assert await _threads_of(world, _Q) == {thread}
    after = await world.record(f"{_Q}-in-3", LINE, thread, _Q1)
    assert await _writers(world, thread) == {_Q}
    assert [await world.lua_env.store.get_record(message_id) for message_id in (*q_ids, between)] == [None] * (
        len(q_ids) + 1
    )
    assert (await world.lua_env.store.get_record(after)).thread_id == thread


@pytest.mark.parametrize("cleared_by", ["thread_delete", "erase"])
async def test_a_write_after_the_drop_stays_recorded(world, monkeypatch, cleared_by):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    m_ids = await _two_address_person(world, _M, _M1, _M2, age_days=2)
    thread, _q_ids = await _single_address_person(world, _Q, _Q1)
    dropped = pause_call(
        monkeypatch,
        world.lua_env.manager.records,
        "drop_thread",
        after=True,
        when=lambda route_name, thread_id: thread_id == thread,
    )
    if cleared_by == "thread_delete":
        clearing = asyncio.ensure_future(ops.delete_conversation_thread(LINE.route_name, thread))
        writer, route, our_identity, survivor = _Q, LINE, LINE_IDENTITY, _P
    else:
        assert (await world.link(_Q, _P)).person_id == _P
        clearing = asyncio.ensure_future(ops.delete_conversation_person(_P))
        writer, route, our_identity, survivor = _R, _LINE_I2, _I2, _M
    await asyncio.wait_for(dropped.reached.wait(), 10)
    if cleared_by == "erase":
        await world.lua_env.manager.put_route(_LINE_I2)
        await world.seed(person(_R, channel_address(_LINE_I2, _Q1)), state=False)
    late = await world.record(f"{writer}-late", route, thread, _Q1, our_identity=our_identity)
    dropped.go.set()
    await asyncio.wait_for(clearing, 30)

    assert await _writers(world, thread) == {writer}
    assert await _threads_of(world, writer) == {thread}
    assert (await world.lua_env.store.get_record(late)).thread_id == thread
    assert (await world.link(writer, survivor)).person_id == survivor

    result = await ops.delete_conversation_person(survivor)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids if survivor == _P else m_ids) + 1
    assert await world.lua_env.store.get_record(late) is None


async def test_a_write_after_a_thread_deletes_drop_is_wiped_by_its_writers_own_erase(world, monkeypatch):  # noqa: F811
    await world.routes(LINE)
    thread, _q_ids = await _single_address_person(world, _Q, _Q1)
    dropped = pause_call(
        monkeypatch,
        world.lua_env.manager.records,
        "drop_thread",
        after=True,
        when=lambda route_name, thread_id: thread_id == thread,
    )
    clearing = asyncio.ensure_future(ops.delete_conversation_thread(LINE.route_name, thread))
    await asyncio.wait_for(dropped.reached.wait(), 10)
    late = await world.record(f"{_Q}-late", LINE, thread, _Q1)
    dropped.go.set()
    await asyncio.wait_for(clearing, 30)
    assert await _writers(world, thread) == {_Q}

    result = await ops.delete_conversation_person(_Q)

    assert result["erased"] is True
    assert result["removed"] == 1
    assert await world.lua_env.store.get_record(late) is None
    assert await _writers(world, thread) == set()
    assert await _threads_of(world, _Q) == set()


@pytest.mark.parametrize("work", ["held_send", "park_completion", "operator_send"])
async def test_the_absorbed_persons_own_work_after_the_link_is_wiped_with_its_thread(
    world,  # noqa: F811
    monkeypatch,
    work,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    held = await world.held_send(_Q1) if work == "held_send" else None
    if work == "park_completion":
        await world.park(f"{_Q}-park", thread)
    assert (await world.link(_Q, _P)).person_id == _P

    if held is not None:
        await held.fail()
        landed = held.message_id
    elif work == "park_completion":
        landed = await _park_completes(world, monkeypatch, thread, f"{_Q}-done")
    else:
        landed = await _operator_send(world, monkeypatch, LINE, _Q1)
    assert (await world.lua_env.store.get_record(landed)).thread_id == thread

    assert await _writers(world, thread) == {_Q, _P}
    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 2
    for message_id in (chat, landed):
        assert await world.lua_env.store.get_record(message_id) is None
    assert await leftovers(world, _P, _Q, chat, landed, threads=(thread,), parks=(f"{_Q}-park",)) == {}


async def test_a_live_intake_in_a_continued_route_thread_is_no_409(world, scheduled):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    assert (await world.link(_Q, _P)).person_id == _P
    detached = await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_Q1)
    n_write = await world.record("n-in-1", LINE, thread, _Q1)
    pending = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-N2", medium_message_id="m-PID-N2", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    assert await world.lua_env.store.thread_has_live_intake(thread)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids)
    for message_id in (chat, n_write, pending):
        assert (await world.lua_env.store.get_record(message_id)).thread_id == thread
    assert await world.lua_env.store.thread_has_live_intake(thread)
    assert await _writers(world, thread) == {_Q, detached.person_id}


async def test_a_live_intake_in_a_thread_continued_after_the_read_is_no_409(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    assert (await world.link(_Q, _P)).person_id == _P
    await world.seed(person(_R, channel_address(_LINE_I2, _Q1)), state=False)
    clearing = pause_call(
        monkeypatch,
        threads_delete_module,
        "_clear_route_thread",
        when=lambda _person, _erased, paused, _caps: paused == thread,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(clearing.reached.wait(), 10)
    await world.lua_env.manager.put_route(_LINE_I2)
    pending = await intake_module.accept(
        "twilio", _I2, _Q1, _Q1, "hi", "PID-R2", medium_message_id="m-PID-R2", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    assert await _writers(world, thread) == {_Q, _R}
    clearing.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids)
    for message_id in (chat, pending):
        assert (await world.lua_env.store.get_record(message_id)).thread_id == thread
    assert await _writers(world, thread) == {_Q, _R}


def _in_flight_message(thread: str) -> str:
    return f"conversation person '{_P}' has a turn in flight in thread '{thread}'; retry once it drains"


async def test_a_live_intake_on_an_absorbed_route_thread_is_a_409_before_the_fifo(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    pending = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-Q2", medium_message_id="m-PID-Q2", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    assert (await world.link(_Q, _P)).person_id == _P
    caps = get_turn_caps()
    reserved: list[str] = []
    real_reserve = caps.reserve_thread_slot

    def _reserve_spy(thread_id: str) -> None:
        reserved.append(thread_id)
        real_reserve(thread_id)

    monkeypatch.setattr(caps, "reserve_thread_slot", _reserve_spy)

    with pytest.raises(ConflictError, match=re.escape(_in_flight_message(thread))):
        await ops.delete_conversation_person(_P)

    assert reserved == []
    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_P)) == 0
    await _answered(world, scheduled)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 2
    assert await leftovers(world, _P, _Q, chat, pending, threads=(thread,)) == {}


async def test_a_live_intake_after_the_early_check_is_a_409_before_the_hold(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    assert (await world.link(_Q, _P)).person_id == _P
    clearing = pause_call(
        monkeypatch,
        threads_delete_module,
        "_clear_route_thread",
        when=lambda _person, _erased, paused, _caps: paused == thread,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(clearing.reached.wait(), 10)
    pending = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-Q3", medium_message_id="m-PID-Q3", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    assert await _writers(world, thread) == {_Q, _P}
    clearing.go.set()

    with pytest.raises(ConflictError, match=re.escape(_in_flight_message(thread))):
        await asyncio.wait_for(erase, 30)

    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_P)) == 0
    assert (await world.lua_env.store.get_record(chat)).thread_id == thread
    await _answered(world, scheduled)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 2
    assert await leftovers(world, _P, _Q, chat, pending, threads=(thread,)) == {}


async def test_a_live_intake_after_the_check_before_the_hold_is_the_409_under_it(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    assert (await world.link(_Q, _P)).person_id == _P
    looks: list[str] = []

    def _the_look_under_the_hold(looked: str, _erased) -> bool:
        # The chat is looked at by the early check, before its hold, and under its hold.
        looks.extend([looked] if looked == thread else [])
        return looked == thread and len(looks) == 3

    held = pause_call(monkeypatch, world.persons, "outside_writer", when=_the_look_under_the_hold)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(held.reached.wait(), 10)
    pending = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-Q9", medium_message_id="m-PID-Q9", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    held.go.set()

    with pytest.raises(ConflictError, match=re.escape(_in_flight_message(thread))):
        await asyncio.wait_for(erase, 30)

    assert await world.redis.exists(world.settings.keyspace.person_erasing_key(_P)) == 0
    assert (await world.lua_env.store.get_record(chat)).thread_id == thread
    await _answered(world, scheduled)

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 2
    assert await leftovers(world, _P, _Q, chat, pending, threads=(thread,)) == {}


async def test_a_send_in_the_erase_window_is_deleted_and_a_message_after_the_drop_stands(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
    caplog,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    thread, chat = await _q_chat_on_line(world)
    assert (await world.link(_Q, _P)).person_id == _P

    def _is_thread(route_name, thread_id):
        return thread_id == thread

    before_drop = pause_call(monkeypatch, world.lua_env.manager.records, "drop_thread", when=_is_thread)
    after_drop = pause_call(monkeypatch, world.lua_env.manager.records, "drop_thread", after=True, when=_is_thread)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(before_drop.reached.wait(), 10)
    held = await world.held_send(_Q1)
    begun = await world.lua_env.store.get_record(held.message_id)
    assert (begun.thread_id, begun.delivery_status) == (thread, DeliveryStatus.SENDING)
    before_drop.go.set()
    await asyncio.wait_for(after_drop.reached.wait(), 10)
    await held.answer(_Q1, LINE_IDENTITY)
    assert await world.lua_env.store.get_record(held.message_id) is None
    assert any(record.levelname == "WARNING" and held.message_id in record.getMessage() for record in caplog.records)
    fresh = await intake_module.accept(
        "twilio", LINE_IDENTITY, _Q1, _Q1, "hi", "PID-Q4", medium_message_id="m-PID-Q4", reply_to_medium_message_id=None
    )
    assert [intake.thread_id for intake in scheduled] == [thread]
    after_drop.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert result["erased"] is True
    assert result["removed"] == len(p_ids) + 1
    assert await world.lua_env.store.get_record(chat) is None
    page, _unreadable = await world.lua_env.store.list_thread_records(LINE.route_name, thread, offset=0, limit=50)
    assert [record.message_id for record in page.records] == [fresh]


async def test_a_chain_of_links_is_erased_whole_and_names_its_last_survivor(world):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=2)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=1)
    r_thread, r_ids = await _single_address_person(world, _R, _R1)
    assert (await world.link(_R, _Q)).person_id == _Q
    assert (await world.link(_Q, _P)).person_id == _P

    for absorbed_id in (_Q, _R):
        assert await world.redis.get(world.settings.keyspace.person_absorbed_into_key(absorbed_id)) == _P
        with pytest.raises(ConflictError, match=re.escape(f"absorbed into person '{_P}'")):
            await ops.delete_conversation_person(absorbed_id)
    assert await world.redis.smembers(world.settings.keyspace.person_absorbed_key(_P)) == {_Q, _R}
    assert r_thread in (await world.persons.read_for_erase(_P)).route_threads

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_Q, _R]
    assert result["removed"] == len(p_ids) + len(q_ids) + len(r_ids)
    assert r_thread in world.saver.deleted
    assert world.states_naming(r_thread) == []
    assert (
        await leftovers(world, _P, _Q, _R, *p_ids, *q_ids, *r_ids, threads=(r_thread,), parks=(f"{_R}-park-thread",))
        == {}
    )
    assert await world.redis.exists(world.settings.keyspace.person_absorbed_key(_P)) == 0


async def test_an_absorbed_persons_thread_is_dropped_on_a_route_its_address_left_afterwards(world):  # noqa: F811
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE, _P1), age_days=2))
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=1)
    assert (await world.link(_Q, _P)).person_id == _P
    await world.persons.detach(_P, door="channel", channel="twilio", our_identity=LINE_B_IDENTITY, address=_Q2)
    q_on_b = [
        message_id
        for message_id in q_ids
        if (await world.lua_env.store.get_record(message_id)).route_name == LINE_B.route_name
    ]
    assert q_on_b
    assert LINE_B.route_name in (await world.persons.read_for_erase(_P)).thread_routes[_Q]
    assert {route for address in (await world.persons.get_by_id(_P)).addresses for route in address.routes} == {
        LINE.route_name
    }

    result = await ops.delete_conversation_person(_P)

    assert result["erased"] is True
    assert result["removed"] == len(q_ids)
    for message_id in q_ids:
        assert await world.lua_env.store.get_record(message_id) is None
