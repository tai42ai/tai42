"""A person thread is torn down on every route its writes recorded, not only its addresses' routes.

An ``/unlink`` takes a route off the person, and a send can land in the person thread on a route
no address names; the person's erase, the erase of a person that absorbed it and a thread delete
each still reach that route. Each case ends with a scan of every store.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.operations import conversations as ops

from .conftest import _channel_route
from .linked_erase import (
    LINE,
    LINE_B,
    LINE_B_IDENTITY,
    assert_clean,
    channel_address,
    leftovers,
    person,
    person_thread_id,
)
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
from .test_person_erase_absorbed_refusals import _parks
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture


async def _unlinked_on_line_b(world, person_id: str, first: str, second: str, *, age_days: int) -> list[str]:  # noqa: F811
    """A person on ``line`` and ``line-b`` whose ``line-b`` address is unlinked after its writes; its ids."""
    ids = await _two_address_person(world, person_id, first, second, age_days=age_days)
    await world.lua_env.manager.modes.set_mode(person_thread_id(person_id), "manual")
    await world.persons.detach(
        person_id, door="channel", channel="twilio", our_identity=LINE_B_IDENTITY, address=second
    )
    assert await world.persons.thread_routes(person_id) == {LINE.route_name, LINE_B.route_name}
    return ids


async def _thread_gone(world, thread: str, *route_names: str) -> None:  # noqa: F811
    settings = world.settings
    for route_name in route_names:
        assert await world.redis.exists(settings.thread_index_key(route_name, thread)) == 0
        assert await world.redis.zscore(settings.route_threads_key(route_name), thread) is None
    assert await world.redis.exists(settings.mode_key(thread)) == 0


async def test_a_persons_erase_reaches_the_route_its_own_unlink_took_off_it(world):  # noqa: F811
    await world.routes(LINE, LINE_B)
    q_ids = await _unlinked_on_line_b(world, _Q, _Q1, _Q2, age_days=0)

    result = await ops.delete_conversation_person(_Q)

    assert (result["erased"], result["removed"]) == (True, len(q_ids))
    thread = person_thread_id(_Q)
    await _thread_gone(world, thread, LINE.route_name, LINE_B.route_name)
    assert await leftovers(world, _Q, *q_ids, threads=(thread,), parks=_parks(_Q)) == {}


async def test_an_erase_reaches_the_route_an_absorbed_person_unlinked_before_the_merge(world):  # noqa: F811
    await world.routes(LINE, LINE_B)
    p_ids = await _two_address_person(world, _P, _P1, _P2, age_days=1)
    q_ids = await _unlinked_on_line_b(world, _Q, _Q1, _Q2, age_days=0)
    assert (await world.link(_Q, _P)).person_id == _P
    assert (await world.persons.read_for_erase(_P)).thread_routes[_Q] == [LINE.route_name, LINE_B.route_name]

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["absorbed_person_ids"]) == (True, [_Q])
    assert result["removed"] == len(p_ids) + len(q_ids)
    await _thread_gone(world, person_thread_id(_Q), LINE.route_name, LINE_B.route_name)
    await assert_clean(
        world,
        _P,
        _Q,
        _P1,
        _P2,
        *p_ids,
        *q_ids,
        threads=(person_thread_id(_P), person_thread_id(_Q)),
        parks=_parks(_P, _Q),
    )


async def test_an_erase_reaches_a_route_only_a_send_to_the_person_used(world):  # noqa: F811
    old, new = _channel_route("line-w", "+15550007777"), _channel_route("line-w2", "+15550007777")
    await world.routes(LINE_B, old)
    await world.seed(person(_P, channel_address(old, _P1), channel_address(LINE_B, _P2)))
    await ops.delete_conversation_route(old.route_name)
    await world.routes(new)
    sent = await world.ask(_P1, f"{_P}-ask", our_identity=new.our_identity)
    thread = person_thread_id(_P)
    assert (await world.lua_env.store.get_record(sent)).thread_id == thread
    assert await world.redis.zrange(world.settings.thread_index_key(new.route_name, thread), 0, -1) == [sent]

    result = await ops.delete_conversation_person(_P)

    assert (result["erased"], result["removed"]) == (True, 1)
    await _thread_gone(world, thread, new.route_name)
    await assert_clean(world, _P, _P1, _P2, sent, threads=(thread,))


@pytest.mark.parametrize("named_on", [LINE.route_name, LINE_B.route_name])
async def test_a_thread_delete_of_a_person_thread_reaches_every_recorded_route(world, named_on):  # noqa: F811
    await world.routes(LINE, LINE_B)
    await world.seed(person(_Q, channel_address(LINE, _Q1), channel_address(LINE_B, _Q2)))
    thread = person_thread_id(_Q)
    ids = [
        await world.record(f"{_Q}-in-1", LINE, thread, _Q1),
        await world.record(f"{_Q}-in-2", LINE_B, thread, _Q2),
    ]
    await world.lua_env.manager.modes.set_mode(thread, "manual")
    await world.persons.detach(_Q, door="channel", channel="twilio", our_identity=LINE_B_IDENTITY, address=_Q2)

    result = await ops.delete_conversation_thread(named_on, thread)

    assert result["removed"] == len(ids)
    await _thread_gone(world, thread, LINE.route_name, LINE_B.route_name)
    assert [message_id for message_id in ids if await world.lua_env.store.get_record(message_id)] == []
