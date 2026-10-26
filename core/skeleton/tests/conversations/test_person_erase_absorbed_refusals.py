"""A person erase over the persons it absorbed when it is refused, interrupted or raced.

A failure after an absorbed person was cleared, a link landing just before the mark, a turn in
flight in an absorbed person's thread, a send to it still going out, and a turn on it holding its
lock while it waits for a gate slot. Each case that finishes ends with a scan of every store.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re

import pytest
from tai42_contract.errors import UnavailableError

from tai42_skeleton.app import instance
from tai42_skeleton.conversations.caps import ThreadBusyError, get_turn_caps
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module
from tai42_skeleton.operations.errors import ConflictError

from .conftest import pause_call
from .linked_erase import LINE, LINE_B, LINE_IDENTITY, assert_clean, person_thread_id, route_thread_id
from .test_person_erase_absorbed import (
    _P,
    _P1,
    _P2,
    _Q,
    _Q1,
    _Q2,
    _answered,
    _two_address_person,
    world,  # noqa: F401  # the linked-erase world fixture
)
from .test_person_erase_sends import scheduled  # noqa: F401  # the doors' turn scheduling, held
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture

_R, _S = "r0000002", "s0000001"
_R1, _R2 = "+15550007001", "+15550007002"
_S1, _S2 = "+15550008001", "+15550008002"


def _parks(*person_ids: str) -> tuple[str, ...]:
    return tuple(f"{person_id}-park-{kind}" for person_id in person_ids for kind in ("thread", "person"))


def _threads(*person_ids: str) -> tuple[str, ...]:
    return tuple(person_thread_id(person_id) for person_id in person_ids)


def _absorbed_into(person_id: str, survivor: str) -> str:
    return (
        f"conversation person {person_id!r} was absorbed into person {survivor!r} by a link and is not erased on "
        f"its own: erase {survivor!r}, which clears {survivor!r} and every person it absorbed, {person_id!r} included"
    )


def _in_flight(absorbed_id: str) -> str:
    return (
        f"conversation person {_P!r} has a turn in flight in thread {person_thread_id(absorbed_id)!r}; "
        "retry once it drains"
    )


async def _exists(world, key: str) -> bool:  # noqa: F811
    return await world.redis.exists(key) == 1


async def _entries(world, person_id: str) -> list[str]:  # noqa: F811
    return sorted(await world.redis.smembers(world.settings.keyspace.person_absorbed_key(person_id)))


async def _pointer(world, person_id: str) -> str | None:  # noqa: F811
    return await world.redis.get(world.settings.keyspace.person_absorbed_into_key(person_id))


async def _standing(world, message_ids: list[str]) -> list[str]:  # noqa: F811
    return [message_id for message_id in message_ids if await world.lua_env.store.get_record(message_id) is not None]


async def _lapsed(world, message_id: str) -> str:  # noqa: F811
    """Lapse an accepted intake's lease, as before its turn took it; answers the lease to put back."""
    key = world.settings.keyspace.record_key(message_id)
    lease = await world.redis.hget(key, "intake_claim")
    token, _sep, _expiry = lease.partition(":")
    await world.redis.hset(key, "intake_claim", f"{token}:0")
    return lease


async def _taken(world, message_id: str, lease: str) -> None:  # noqa: F811
    """The intake's lease taken again: its turn is live in its thread."""
    await world.redis.hset(world.settings.keyspace.record_key(message_id), "intake_claim", lease)


async def _accepted(scheduled, address: str, provider_message_id: str) -> str:  # noqa: F811
    """An inbound from ``address`` on ``line``, accepted with its turn not yet run."""
    return await intake_module.accept(
        "twilio",
        LINE_IDENTITY,
        address,
        address,
        "hi",
        provider_message_id,
        medium_message_id=None,
        reply_to_medium_message_id=None,
    )


def _tight_caps(monkeypatch, **bounds) -> None:
    """Run this worker's turn caps on the current settings with ``bounds`` changed."""
    caps = get_turn_caps()
    current = caps.settings.model_copy(update=bounds)
    monkeypatch.setattr(caps, "_settings", lambda: current)
    caps.reconfigure()


async def test_an_interrupted_erase_with_an_absorbed_person_is_finished_by_its_retry(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    assert (await world.link(_Q, _P)).person_id == _P
    real_state_erase = threads_delete_module._erase_person_state
    calls: list[str] = []

    async def _failing_once(*args, **kwargs):
        calls.append("state")
        if len(calls) == 1:
            raise RuntimeError("the states store is unreachable")
        return await real_state_erase(*args, **kwargs)

    monkeypatch.setattr(threads_delete_module, "_erase_person_state", _failing_once)

    with pytest.raises(RuntimeError, match="the states store is unreachable"):
        await ops.delete_conversation_person(_P)

    settings = world.settings
    assert await _exists(world, settings.keyspace.person_key(_P))
    assert await _exists(world, settings.keyspace.person_erasing_key(_P))
    assert await _entries(world, _P) == [_Q]
    assert await _standing(world, q_ids) == []
    assert person_thread_id(_Q) in world.saver.deleted

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q])
    assert not await _exists(world, settings.keyspace.person_absorbed_key(_P))
    await assert_clean(
        world, _P, _Q, _P1, _P2, _Q1, _Q2, *p_ids, *q_ids, threads=_threads(_P, _Q), parks=_parks(_P, _Q)
    )


async def test_a_link_landing_just_before_the_mark_is_erased_with_the_person(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    marking = pause_call(monkeypatch, world.persons, "mark_erasing")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(marking.reached.wait(), 10)
    assert (await world.link(_Q, _P)).person_id == _P
    marking.go.set()

    result = await asyncio.wait_for(erase, 30)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q])
    await assert_clean(
        world, _P, _Q, _P1, _P2, _Q1, _Q2, *p_ids, *q_ids, threads=_threads(_P, _Q), parks=_parks(_P, _Q)
    )


async def test_a_person_merged_away_before_its_mark_is_refused_and_its_survivor_erases_both(
    world,  # noqa: F811
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    s_ids = await _two_address_person(world, _S, _S1, _S2, age_days=2)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    assert (await world.link(_Q, _P)).person_id == _P
    marking = pause_call(monkeypatch, world.persons, "mark_erasing")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(marking.reached.wait(), 10)
    assert (await world.link(_P, _S)).person_id == _S
    marking.go.set()

    with pytest.raises(ConflictError) as refused:
        await asyncio.wait_for(erase, 30)

    assert (str(refused.value), refused.value.extra) == (_absorbed_into(_P, _S), {"absorbed_into_person_id": _S})
    assert await _standing(world, [*p_ids, *q_ids]) == [*p_ids, *q_ids]
    assert world.saver.deleted == []
    assert await _entries(world, _S) == [_P, _Q]
    assert (await _pointer(world, _P), await _pointer(world, _Q)) == (_S, _S)
    with pytest.raises(ConflictError) as q_refused:
        await ops.delete_conversation_person(_Q)
    assert (str(q_refused.value), q_refused.value.extra) == (_absorbed_into(_Q, _S), {"absorbed_into_person_id": _S})

    result = await ops.delete_conversation_person(_S)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_P, _Q])
    assert (await _pointer(world, _P), await _pointer(world, _Q)) == (None, None)
    await assert_clean(
        world,
        _S,
        _P,
        _Q,
        _S1,
        _S2,
        _P1,
        _P2,
        _Q1,
        _Q2,
        *s_ids,
        *p_ids,
        *q_ids,
        threads=_threads(_S, _P, _Q),
        parks=_parks(_S, _P, _Q),
    )


async def _p_absorbed_q_and_r(world, scheduled) -> tuple[list[str], str, str]:  # noqa: F811
    """P absorbed Q and R by two links; R's inbound accepted into its own thread before them, its
    lease lapsed. Answers every message id, the inbound's and the lease to put back."""
    await world.routes(LINE, LINE_B)
    ids = [
        *await _two_address_person(world, _P, _P1, _P2, age_days=2),
        *await _two_address_person(world, _Q, _Q1, _Q2, age_days=1),
        *await _two_address_person(world, _R, _R1, _R2, age_days=0),
    ]
    pending = await _accepted(scheduled, _R1, "PID-R1")
    assert [intake.thread_id for intake in scheduled] == [person_thread_id(_R)]
    lease = await _lapsed(world, pending)
    assert (await world.link(_Q, _P)).person_id == _P
    assert (await world.link(_R, _P)).person_id == _P
    return ids, pending, lease


async def test_a_turn_live_in_an_absorbed_thread_after_the_early_checks_refuses_and_a_retry_finishes(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    ids, pending, lease = await _p_absorbed_q_and_r(world, scheduled)
    settings = world.settings
    recorded_routes = await world.redis.smembers(settings.keyspace.person_thread_routes_key(_P))
    assert recorded_routes == {LINE.route_name, LINE_B.route_name}
    clearing = pause_call(
        monkeypatch,
        threads_delete_module,
        "_clear_absorbed_thread",
        when=lambda _person, absorbed_id, _routes, _caps: absorbed_id == _R,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(clearing.reached.wait(), 10)
    await _taken(world, pending, lease)
    clearing.go.set()

    with pytest.raises(ConflictError, match=re.escape(_in_flight(_R))):
        await asyncio.wait_for(erase, 30)

    assert not await _exists(world, settings.keyspace.person_erasing_key(_P))
    assert await _exists(world, settings.keyspace.person_key(_P))
    assert await _entries(world, _P) == [_Q, _R]
    assert await world.redis.smembers(settings.keyspace.person_thread_routes_key(_P)) == recorded_routes
    assert await _standing(world, [i for i in ids if i.startswith(_Q)]) == []
    assert await world.lua_env.store.get_record(pending) is not None
    await _answered(world, scheduled)

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q, _R])
    await assert_clean(
        world,
        _P,
        _Q,
        _R,
        _P1,
        _P2,
        _Q1,
        _Q2,
        _R1,
        _R2,
        *ids,
        pending,
        threads=_threads(_P, _Q, _R),
        parks=_parks(_P, _Q, _R),
    )


async def test_an_unsettled_save_after_the_persons_own_teardown_keeps_the_mark_and_a_retry_finishes(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    ids, pending, _lease = await _p_absorbed_q_and_r(world, scheduled)
    await _answered(world, scheduled)
    service = instance.app._states_service
    real_drain = service.drain_target_saves
    drains: list[str] = []

    async def _unsettled_once(target_kind, target_name):
        drains.append(target_name)
        if len(drains) == 1:
            return ["a pending save of 'assistant_memory' is unsettled"]
        return await real_drain(target_kind, target_name)

    monkeypatch.setattr(service, "drain_target_saves", _unsettled_once)

    with pytest.raises(UnavailableError, match="pending saves are unsettled"):
        await ops.delete_conversation_person(_P)

    settings = world.settings
    assert await _exists(world, settings.keyspace.person_erasing_key(_P))
    assert await _exists(world, settings.keyspace.person_key(_P))
    assert await _standing(world, ids) == []
    marks: list[bool] = []
    real_mark = world.persons.mark_erasing

    async def _mark_spy(person_id: str) -> bool:
        marks.append(await real_mark(person_id))
        return marks[-1]

    monkeypatch.setattr(world.persons, "mark_erasing", _mark_spy)

    result = await ops.delete_conversation_person(_P)

    assert marks == [True]
    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q, _R])
    await assert_clean(
        world,
        _P,
        _Q,
        _R,
        _P1,
        _P2,
        _Q1,
        _Q2,
        _R1,
        _R2,
        *ids,
        pending,
        threads=_threads(_P, _Q, _R),
        parks=_parks(_P, _Q, _R),
    )


async def test_a_turn_in_flight_in_an_absorbed_thread_refuses_the_erase_before_its_fifo(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    await _accepted(scheduled, _Q1, "PID-Q1")
    assert [intake.thread_id for intake in scheduled] == [person_thread_id(_Q)]
    assert (await world.link(_Q, _P)).person_id == _P
    caps = get_turn_caps()
    reserved: list[str] = []
    real_reserve = caps.reserve_thread_slot

    def _reserve_spy(thread_id: str) -> None:
        reserved.append(thread_id)
        real_reserve(thread_id)

    monkeypatch.setattr(caps, "reserve_thread_slot", _reserve_spy)

    with pytest.raises(ConflictError, match=re.escape(_in_flight(_Q))):
        await ops.delete_conversation_person(_P)

    assert reserved == []
    assert not await _exists(world, world.settings.keyspace.person_erasing_key(_P))


async def test_a_row_gone_after_the_mark_raises_and_leaves_the_mark(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _P, _P1, _P2, age_days=0)
    real_read = world.persons.read_for_erase
    reads: list[str] = []

    async def _row_gone_after_the_first(person_id: str):
        reads.append(person_id)
        read = await real_read(person_id)
        return read if len(reads) == 1 else dataclasses.replace(read, person=None)

    monkeypatch.setattr(world.persons, "read_for_erase", _row_gone_after_the_first)

    with pytest.raises(RuntimeError, match=re.escape(f"person {_P!r} lost its row after its erase mark was set")):
        await ops.delete_conversation_person(_P)

    assert reads == [_P, _P]
    assert await _exists(world, world.settings.keyspace.person_erasing_key(_P))


async def test_a_turn_live_in_an_absorbed_thread_after_the_early_check_gives_the_person_back(
    world,  # noqa: F811
    monkeypatch,
    scheduled,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    pending = await _accepted(scheduled, _Q1, "PID-Q1")
    lease = await _lapsed(world, pending)
    survivor = await world.link(_Q, _P)
    index = await world.index()
    absorbed_ids = await world.redis.smembers(world.settings.keyspace.person_absorbed_key(_P))
    states = world.states_naming(_P, person_thread_id(_P))
    marking = pause_call(monkeypatch, world.persons, "mark_erasing")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(marking.reached.wait(), 10)
    await _taken(world, pending, lease)
    marking.go.set()

    with pytest.raises(ConflictError) as refused:
        await asyncio.wait_for(erase, 30)

    assert str(refused.value) == _in_flight(_Q)
    assert not await _exists(world, world.settings.keyspace.person_erasing_key(_P))
    assert await world.persons.get_by_id(_P) == survivor
    assert await _standing(world, p_ids) == p_ids
    assert world.states_naming(_P, person_thread_id(_P)) == states
    assert await world.redis.smembers(world.settings.keyspace.person_absorbed_key(_P)) == absorbed_ids
    assert await world.index() == index
    assert set(index.values()) == {_P}


async def test_a_turn_holding_an_absorbed_thread_past_the_bound_is_the_503_and_gives_the_person_back(
    world,  # noqa: F811
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    survivor = await world.link(_Q, _P)
    _tight_caps(monkeypatch, sync_door_wait_seconds=0.1)
    caps = get_turn_caps()
    held, release = asyncio.Event(), asyncio.Event()

    async def _turn() -> None:
        caps.reserve_thread_slot(person_thread_id(_Q))
        async with caps.run_reserved(person_thread_id(_Q)):
            held.set()
            await release.wait()

    turn = asyncio.ensure_future(_turn())
    await asyncio.wait_for(held.wait(), 10)

    with pytest.raises(ThreadBusyError, match=re.escape(person_thread_id(_Q))) as refused:
        await asyncio.wait_for(ops.delete_conversation_person(_P), 10)

    assert refused.value.status == 503
    assert not await _exists(world, world.settings.keyspace.person_erasing_key(_P))
    assert await world.persons.get_by_id(_P) == survivor
    release.set()
    await asyncio.wait_for(turn, 10)


async def test_a_send_to_an_absorbed_thread_still_going_out_is_deleted_at_its_end(world, caplog):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    held = await world.held_send(_Q1)
    assert (await world.lua_env.store.get_record(held.message_id)).thread_id == person_thread_id(_Q)
    assert (await world.link(_Q, _P)).person_id == _P

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q])
    with caplog.at_level(logging.WARNING):
        await held.fail()
    assert await world.lua_env.store.get_record(held.message_id) is None
    assert any(record.levelname == "WARNING" and held.message_id in record.getMessage() for record in caplog.records)
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
        held.message_id,
        threads=_threads(_P, _Q),
        parks=_parks(_P, _Q),
    )


async def test_a_send_to_an_absorbed_thread_completing_after_the_erase_lands_in_its_addresss_own_thread(
    world,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    held = await world.held_send(_Q1)
    assert (await world.link(_Q, _P)).person_id == _P
    assert (await ops.delete_conversation_person(_P))["absorbed_person_ids"] == [_Q]

    await held.answer(_Q1, LINE_IDENTITY)

    landed = await world.lua_env.store.get_record(held.message_id)
    assert landed.thread_id == route_thread_id(LINE.route_name, _Q1)
    await assert_clean(
        world,
        _P,
        _Q,
        *p_ids,
        *q_ids,
        threads=_threads(_P, _Q),
        parks=_parks(_P, _Q),
        standing=(held.message_id,),
    )


async def test_a_turn_waiting_for_the_gate_on_an_absorbed_thread_is_the_503_then_runs_and_a_retry_finishes(
    world,  # noqa: F811
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    assert (await world.link(_Q, _P)).person_id == _P
    _tight_caps(monkeypatch, sync_door_wait_seconds=0.1, max_concurrent_turns=1)
    caps = get_turn_caps()
    clearing = pause_call(monkeypatch, threads_delete_module, "_clear_absorbed_thread")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(clearing.reached.wait(), 10)
    ran = asyncio.Event()

    async def _turn() -> None:
        caps.reserve_thread_slot(person_thread_id(_Q))
        async with caps.run_reserved(person_thread_id(_Q)):
            ran.set()

    turn = asyncio.ensure_future(_turn())
    for _ in range(100):
        if (
            caps._thread_locks.get(person_thread_id(_Q)) is not None
            and caps._thread_locks[person_thread_id(_Q)].locked()
        ):
            break
        await asyncio.sleep(0.01)
    assert not ran.is_set()
    clearing.go.set()

    with pytest.raises(ThreadBusyError, match=re.escape(person_thread_id(_Q))):
        await asyncio.wait_for(erase, 30)

    assert not await _exists(world, world.settings.keyspace.person_erasing_key(_P))
    await asyncio.wait_for(turn, 10)
    assert ran.is_set()

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q])
    await assert_clean(
        world, _P, _Q, _P1, _P2, _Q1, _Q2, *p_ids, *q_ids, threads=_threads(_P, _Q), parks=_parks(_P, _Q)
    )
