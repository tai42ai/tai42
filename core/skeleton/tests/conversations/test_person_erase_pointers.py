"""A person erase given an id a link absorbed, and the absorbed-into pointer that names its survivor.

An absorbed id is refused with a 409 naming the person whose erase clears it, whether the link
landed before the call, along a chain or before an unlink. A dangling pointer is loud. A
``delete-person`` call of an absorbed id holding that id's thread, and a redeem's park re-key
landing during the survivor's erase, each leave nothing behind.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from tai42_skeleton.conversations.caps import ThreadBusyError, get_turn_caps
from tai42_skeleton.interactions import helper as interactions_helper
from tai42_skeleton.interactions.helper import rekey_park_subjects
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import ConflictError

from .conftest import Pause, pause_call
from .linked_erase import LINE, LINE_B, LINE_IDENTITY, assert_clean, person_thread_id
from .test_person_erase_absorbed import (
    _P,
    _P1,
    _P2,
    _Q,
    _Q1,
    _Q2,
    _two_address_person,
    world,  # noqa: F401  # the linked-erase world fixture
)
from .test_person_erase_absorbed_refusals import _R, _R1, _R2, _S, _S1, _S2, _absorbed_into, _parks, _threads
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture


async def _pointer(world, person_id: str) -> str | None:  # noqa: F811
    return await world.redis.get(world.settings.person_absorbed_into_key(person_id))


async def _snapshot(world) -> tuple:  # noqa: F811
    """Every conversations key with its value, the states rows and the standing parks of P and S."""
    parks = {park: await world.park_stands(park) for park in _parks(_P, _S)}
    return await world.dump(), sorted(world.states_pg.records), parks


def _gone(person_id: str) -> dict:
    return {"person_id": person_id, "removed": 0, "erased": False, "states_erased": 0, "absorbed_person_ids": []}


async def test_an_id_absorbed_before_the_call_is_refused_naming_its_survivor_with_nothing_touched(
    world,  # noqa: F811
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _S, _S1, _S2, age_days=2)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    assert (await world.persons.merge(_P, _S)).person_id == _S
    caps = get_turn_caps()
    reserved: list[str] = []
    monkeypatch.setattr(caps, "reserve_thread_slot", reserved.append)
    before = await _snapshot(world)

    with pytest.raises(ConflictError) as refused:
        await ops.delete_conversation_person(_P)

    assert str(refused.value) == _absorbed_into(_P, _S)
    assert refused.value.extra == {"absorbed_into_person_id": _S}
    assert reserved == []
    assert await _snapshot(world) == before
    assert world.saver.deleted == []
    assert all(before[2].values())


async def test_every_id_of_a_chain_points_at_its_last_survivor_until_its_erase(world):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=2)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=1)
    r_ids = await _two_address_person(world, _R, _R1, _R2, age_days=0)
    assert (await world.link(_R, _Q)).person_id == _Q
    assert (await world.link(_Q, _P)).person_id == _P
    assert (await _pointer(world, _R), await _pointer(world, _Q)) == (_P, _P)
    for absorbed_id in (_R, _Q):
        with pytest.raises(ConflictError) as refused:
            await ops.delete_conversation_person(absorbed_id)
        assert (str(refused.value), refused.value.extra) == (
            _absorbed_into(absorbed_id, _P),
            {"absorbed_into_person_id": _P},
        )

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q, _R])
    assert (await _pointer(world, _R), await _pointer(world, _Q)) == (None, None)
    assert await ops.delete_conversation_person(_R) == _gone(_R)
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
        *p_ids,
        *q_ids,
        *r_ids,
        threads=_threads(_P, _Q, _R),
        parks=_parks(_P, _Q, _R),
    )


async def test_an_unlink_after_the_merge_keeps_the_pointer_and_the_fresh_person_erases_on_its_own(
    world,  # noqa: F811
):
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _S, _S1, _S2, age_days=2)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    assert (await world.link(_P, _S)).person_id == _S
    fresh = await world.persons.detach(_S, door="channel", channel="twilio", our_identity=LINE_IDENTITY, address=_P1)

    assert await _pointer(world, _P) == _S
    with pytest.raises(ConflictError) as refused:
        await ops.delete_conversation_person(_P)
    assert (str(refused.value), refused.value.extra) == (_absorbed_into(_P, _S), {"absorbed_into_person_id": _S})
    assert await _pointer(world, fresh.person_id) is None

    result = await ops.delete_conversation_person(fresh.person_id)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [])
    assert await world.persons.get_by_id(_S) is not None
    assert await _pointer(world, _P) == _S


async def test_a_stale_id_and_an_id_that_never_existed_answer_nothing_erased(world):  # noqa: F811
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    assert (await ops.delete_conversation_person(_P))["erased"] is True
    never = "z0000009"

    assert await ops.delete_conversation_person(_P) == _gone(_P)
    assert await ops.delete_conversation_person(never) == _gone(never)
    for person_id in (_P, never):
        assert await world.redis.exists(world.settings.person_absorbed_into_key(person_id)) == 0


async def test_a_dangling_pointer_is_loud_and_the_erase_touches_nothing(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await world.redis.set(world.settings.person_absorbed_into_key(_P), "x-gone")
    message = re.escape(repr(_P)) + ".*" + re.escape(repr("x-gone"))
    with pytest.raises(RuntimeError, match=message):
        await world.persons.read_for_erase(_P)
    caps = get_turn_caps()
    reserved: list[str] = []
    monkeypatch.setattr(caps, "reserve_thread_slot", reserved.append)
    before = await world.dump(), sorted(world.states_pg.records)

    with pytest.raises(RuntimeError, match=message):
        await ops.delete_conversation_person(_P)

    assert (await world.dump(), sorted(world.states_pg.records)) == before
    assert reserved == []
    assert world.saver.deleted == []
    assert await world.park_stands(f"{_P}-park-thread")


class _HoldSpy:
    """Records each thread the erase asks ``hold_thread`` for, and when it asked for the watched one."""

    def __init__(self, monkeypatch, watched: str) -> None:
        caps = get_turn_caps()
        real = caps.hold_thread
        self.reached = asyncio.Event()

        def _hold(thread_id, **kwargs):
            if thread_id == watched:
                self.reached.set()
            return real(thread_id, **kwargs)

        monkeypatch.setattr(caps, "hold_thread", _hold)


async def _q_call_paused_after_its_pointer_read(world, monkeypatch) -> tuple[asyncio.Future, Pause]:  # noqa: F811
    """A ``delete-person Q`` started before Q is linked into P, paused under Q's FIFO after its pointer read."""
    marking = pause_call(monkeypatch, world.persons, "mark_erasing", when=lambda person_id: person_id == _Q)
    call = asyncio.ensure_future(ops.delete_conversation_person(_Q))
    await asyncio.wait_for(marking.reached.wait(), 10)
    assert (await world.link(_Q, _P)).person_id == _P
    reading = pause_call(
        monkeypatch, world.persons, "read_for_erase", after=True, when=lambda person_id: person_id == _Q
    )
    marking.go.set()
    await asyncio.wait_for(reading.reached.wait(), 10)
    return call, reading


async def test_an_erase_waits_out_a_delete_person_call_holding_an_absorbed_thread(world, monkeypatch):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    q_call, reading = await _q_call_paused_after_its_pointer_read(world, monkeypatch)
    holding = _HoldSpy(monkeypatch, person_thread_id(_Q))

    erase = asyncio.ensure_future(ops.delete_conversation_person(_P))
    await asyncio.wait_for(holding.reached.wait(), 10)
    await asyncio.sleep(0.2)
    assert not erase.done()
    reading.go.set()

    with pytest.raises(ConflictError) as refused:
        await asyncio.wait_for(q_call, 10)
    assert str(refused.value) == _absorbed_into(_Q, _P)
    result = await asyncio.wait_for(erase, 30)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q])
    await assert_clean(
        world, _P, _Q, _P1, _P2, _Q1, _Q2, *p_ids, *q_ids, threads=_threads(_P, _Q), parks=_parks(_P, _Q)
    )


async def test_an_erase_behind_a_delete_person_call_past_the_bound_is_the_503_with_its_mark_gone(
    world,  # noqa: F811
    monkeypatch,
):
    await world.routes(LINE, LINE_B)
    await _two_address_person(world, _P, _P1, _P2, age_days=1)
    await _two_address_person(world, _Q, _Q1, _Q2, age_days=0)
    q_call, reading = await _q_call_paused_after_its_pointer_read(world, monkeypatch)
    caps = get_turn_caps()
    current = caps.settings.model_copy(update={"sync_door_wait_seconds": 0.1})
    monkeypatch.setattr(caps, "_settings", lambda: current)
    caps.reconfigure()

    with pytest.raises(ThreadBusyError, match=re.escape(person_thread_id(_Q))) as refused:
        await asyncio.wait_for(ops.delete_conversation_person(_P), 10)

    assert refused.value.status == 503
    assert await world.redis.exists(world.settings.person_erasing_key(_P)) == 0
    assert await world.persons.get_by_id(_P) is not None
    reading.go.set()
    with pytest.raises(ConflictError):
        await asyncio.wait_for(q_call, 10)


@pytest.mark.parametrize("rekeyed", ["before_the_absorbed_cancel", "between_the_cancels", "after_both_cancels"])
async def test_a_redeems_park_rekey_during_the_survivors_erase_leaves_no_park(world, monkeypatch, rekeyed):  # noqa: F811
    await world.routes(LINE, LINE_B)
    s_ids = await _two_address_person(world, _S, _S1, _S2, age_days=2)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    # The redeem's merge has landed; its re-key of P's parks onto S is still to run.
    assert (await world.persons.merge(_P, _S)).person_id == _S
    watched = person_thread_id(_P if rekeyed == "before_the_absorbed_cancel" else _S)
    cancelling = pause_call(
        monkeypatch,
        interactions_helper,
        "cancel_parks_for_thread",
        after=rekeyed == "after_both_cancels",
        when=lambda thread_id, **_kwargs: thread_id == watched,
    )
    erase = asyncio.ensure_future(ops.delete_conversation_person(_S))
    await asyncio.wait_for(cancelling.reached.wait(), 10)

    await rekey_park_subjects([("person", _P, _S), ("thread", person_thread_id(_P), person_thread_id(_S))])
    cancelling.go.set()
    result = await asyncio.wait_for(erase, 30)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_P])
    assert [park for park in _parks(_P, _S) if await world.park_stands(park)] == []
    assert world.interaction_keys_naming(_P, _S, *_threads(_P, _S)) == []
    await assert_clean(
        world, _S, _P, _S1, _S2, _P1, _P2, *s_ids, *p_ids, threads=_threads(_S, _P), parks=_parks(_S, _P)
    )
