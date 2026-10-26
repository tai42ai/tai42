"""Whether a writer outside an erase keeps a route-keyed thread standing, at the script level.

The decision reads the ``writer`` of each record the thread's transcript index holds and judges it
against the person rows, the erase marks and the absorbed-into pointers as they stand. The erase's
final step refuses with a 409 while a visit list of the erased ids names a thread the erase did not
visit, and otherwise adds each visited thread that no writer outside keeps standing and that still
holds a record of the erased ids to the visit list of that record's door. Each case runs the real
script text on ``fakeredis[lua]`` and the fake record client's mirror.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.exceptions import ResponseError
from tai42_contract.conversations import Person, PersonAddress

from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations.persons import ConversationPersonStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.operations.errors import ConflictError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_THREAD = route_thread_id("line", "+15550002001")
_OTHER = route_thread_id("line", "+15550005001")
_P, _Q, _X, _S = "p0000001", "q0000001", "x0000001", "s0000001"
_DOOR = '["agent","echo","[\\"twilio\\",\\"+15550001111\\",\\"+15550002001\\"]"]'
_DOOR_B = '["agent","echo","[\\"twilio\\",\\"+15550001111\\",\\"+15550002002\\"]"]'
_DOOR_C = '["agent","echo","[\\"twilio\\",\\"+15550001111\\",\\"+15550002003\\"]"]'


@dataclass
class Store:
    """The person store over one client, and the seeding steps the cases plant state through."""

    persons: ConversationPersonStore
    client: Any
    torn: type[Exception]

    @property
    def settings(self) -> ConversationsSettings:
        return self.persons.settings

    async def record(
        self, message_id: str, writer: str | None, *, door: str | None = _DOOR, score: float = 1.0
    ) -> None:
        """A record of ``_THREAD`` written by ``writer`` through ``door``; no field for a ``None``."""
        await self.client.zadd(self.settings.keyspace.thread_index_key("line", _THREAD), {message_id: score})
        key = self.settings.keyspace.record_key(message_id)
        await self.client.hset(key, "message_id", message_id)
        if door is not None:
            await self.client.hset(key, "writer_door", door)
        if writer is not None:
            await self.client.hset(key, "writer", writer)

    async def indexed_only(self, message_id: str) -> None:
        """A member of ``_THREAD``'s index whose record row is gone."""
        await self.client.zadd(self.settings.keyspace.thread_index_key("line", _THREAD), {message_id: 9.0})

    async def row(self, person_id: str) -> None:
        await self.client.set(self.settings.keyspace.person_key(person_id), "{}")

    async def mark(self, person_id: str) -> None:
        await self.client.set(self.settings.keyspace.person_erasing_key(person_id), "1")

    async def pointer(self, person_id: str, survivor: str) -> None:
        await self.client.set(self.settings.keyspace.person_absorbed_into_key(person_id), survivor)

    async def visits(self, writer: str, *threads: str) -> None:
        key = self.settings.keyspace.writer_threads_key(writer)
        if isinstance(self.client, FakeRecordRedis):
            for thread in threads:
                self.client._sadd(key, thread)
        else:
            await self.client.sadd(key, *threads)

    async def outside(self, *erased: str) -> str | None:
        return await self.persons.outside_writer(_THREAD, set(erased))


@pytest.fixture(params=["lua", "fake"])
def store(request, monkeypatch) -> Store:
    if request.param == "lua":
        env = request.getfixturevalue("lua_env")
        return Store(persons=env.manager.persons, client=env.redis, torn=ResponseError)
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    fake = FakeRecordRedis()
    monkeypatch.setattr(persons_module, "client_ctx", make_record_client_ctx(fake))
    return Store(persons=ConversationPersonStore(ConversationsSettings()), client=fake, torn=RuntimeError)


# -- the decision -----------------------------------------------------------------------------


async def test_a_thread_with_no_records_has_no_outside_writer(store):
    assert await store.outside(_P) is None


async def test_the_erased_ids_writes_keep_nothing_standing(store):
    await store.row(_P)
    await store.record("m-1", _P)
    await store.record("m-2", _Q)

    assert await store.outside(_P, _Q) is None


async def test_a_door_nobody_held_when_it_wrote_keeps_the_thread(store):
    await store.record("m-1", _DOOR)

    assert await store.outside(_P) == _DOOR


async def test_a_standing_person_keeps_the_thread_until_its_erase_is_marked(store):
    await store.row(_X)
    await store.record("m-1", _X)
    assert await store.outside(_P) == _X

    await store.mark(_X)

    assert await store.outside(_P) is None


async def test_a_person_with_neither_row_nor_pointer_was_erased(store):
    await store.record("m-1", _X)

    assert await store.outside(_P) is None


@pytest.mark.parametrize(
    ("survivor_state", "keeps"),
    [("standing", True), ("marked", False), ("erased_with_the_erase", False)],
)
async def test_an_absorbed_writer_is_judged_by_its_survivor(store, survivor_state, keeps):
    await store.record("m-1", _X)
    await store.pointer(_X, _S)
    await store.row(_S)
    if survivor_state == "marked":
        await store.mark(_S)
    erased = (_P, _S) if survivor_state == "erased_with_the_erase" else (_P,)

    assert await store.outside(*erased) == (_X if keeps else None)


async def test_the_first_standing_writer_in_the_transcript_order_is_the_answer(store):
    await store.row(_X)
    await store.row(_Q)
    await store.record("m-late", _Q, score=2.0)
    await store.record("m-early", _X, score=1.0)
    await store.record("m-own", _P, score=0.5)

    assert await store.outside(_P) == _X


async def test_an_index_member_whose_record_expired_counts_for_nothing(store):
    await store.record("m-1", _P)
    await store.indexed_only("m-gone")

    assert await store.outside(_P) is None


async def test_a_record_without_a_writer_is_torn(store):
    await store.record("m-1", None)

    with pytest.raises(store.torn, match="carries no writer"):
        await store.outside(_P)


async def test_a_pointer_whose_survivor_row_is_gone_is_torn(store):
    await store.record("m-1", _X)
    await store.pointer(_X, _S)

    with pytest.raises(store.torn, match=f"the writer {_X} points at the survivor {_S}, whose row is gone"):
        await store.outside(_P)


async def test_a_thread_that_is_not_route_keyed_is_refused(store):
    with pytest.raises(TypeError, match="not a route-keyed thread"):
        await store.persons.outside_writer(person_thread_id(_P), {_P})


# -- the erase's final step -------------------------------------------------------------------


def _person(person_id: str) -> Person:
    address = PersonAddress(
        door="channel",
        routes=["line"],
        channel="twilio",
        our_identity="+15550001111",
        address="+15550002001",
        linked_at=datetime.now(UTC),
    )
    return Person(
        person_id=person_id, target_kind="agent", target_name="echo", created_at=datetime.now(UTC), addresses=[address]
    )


async def test_the_final_step_refuses_while_a_visit_list_names_a_thread_the_erase_did_not_visit(store):
    await store.row(_P)
    await store.mark(_P)
    await store.pointer(_Q, _P)
    await store.visits(_P, _THREAD)
    await store.visits(_Q, _OTHER)

    unvisited = f"a write by {_Q!r} in the thread {_OTHER!r} landed while the erase of {_P!r} ran"
    with pytest.raises(ConflictError, match=re.escape(unvisited)):
        await store.persons.erase(_person(_P), [_Q], [_THREAD])

    assert await store.client.exists(store.settings.keyspace.person_key(_P)) == 1
    assert await store.client.exists(store.settings.keyspace.person_erasing_key(_P)) == 1
    assert await store.client.exists(store.settings.keyspace.person_absorbed_into_key(_Q)) == 1
    assert await store.client.smembers(store.settings.keyspace.writer_threads_key(_Q)) == {_OTHER}


async def test_the_final_step_removes_the_visit_lists_of_every_visited_thread(store):
    await store.row(_P)
    await store.mark(_P)
    await store.pointer(_Q, _P)
    await store.visits(_P, _THREAD)
    await store.visits(_Q, _OTHER)

    assert await store.persons.erase(_person(_P), [_Q], [_OTHER, _THREAD]) == (True, 0)

    for key in (
        store.settings.keyspace.person_key(_P),
        store.settings.keyspace.person_erasing_key(_P),
        store.settings.keyspace.person_absorbed_into_key(_Q),
        store.settings.keyspace.writer_threads_key(_P),
        store.settings.keyspace.writer_threads_key(_Q),
    ):
        assert await store.client.exists(key) == 0


@pytest.mark.parametrize("kept_by_a_standing_writer", [False, True])
async def test_the_final_step_adds_each_visited_thread_holding_an_erased_write_to_that_writes_door(
    store, kept_by_a_standing_writer
):
    await store.row(_P)
    await store.mark(_P)
    await store.pointer(_Q, _P)
    await store.visits(_P, _THREAD)
    await store.record("m-p", _P)
    await store.record("m-q", _Q, door=_DOOR_B)
    await store.record("m-x", _X, door=_DOOR_C)
    if kept_by_a_standing_writer:
        await store.row(_X)

    assert await store.persons.erase(_person(_P), [_Q], [_OTHER, _THREAD]) == (True, 0)

    handed = set() if kept_by_a_standing_writer else {_THREAD}
    assert await store.client.smembers(store.settings.keyspace.writer_threads_key(_DOOR)) == handed
    assert await store.client.smembers(store.settings.keyspace.writer_threads_key(_DOOR_B)) == handed
    assert await store.client.exists(store.settings.keyspace.writer_threads_key(_DOOR_C)) == 0
    for message_id, writer in (("m-p", _P), ("m-q", _Q), ("m-x", _X)):
        assert await store.client.hget(store.settings.keyspace.record_key(message_id), "writer") == writer


async def test_the_final_step_refuses_an_erased_write_without_its_door(store):
    await store.row(_P)
    await store.mark(_P)
    await store.record("m-p", _P, door=None)

    with pytest.raises(store.torn, match="carries no writer_door"):
        await store.persons.erase(_person(_P), [], [_THREAD])

    assert await store.client.exists(store.settings.keyspace.person_key(_P)) == 1
