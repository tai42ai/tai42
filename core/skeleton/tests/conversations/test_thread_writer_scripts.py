"""Who wrote into a route-keyed thread, at the store's script level.

Every record written into a route-keyed thread keeps its writer on its row and adds the thread to
the writer's visit list in its own atomic step: the writer is the person the target's index names
for the record's door, or the door itself while nobody holds it. A person's first contact adopts its
door's records and visit list, a link records the absorbed ids, and the erase reads the visit lists
of the person and the ids it absorbed and removes them in its last step. Each step runs the real
script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import json
import re

import pytest
from tai42_contract.conversations import CrossTargetMergeError, PersonAddress

from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.persons import PairingTarget, PersonErasingError, _address_key_of, thread_door
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.conversations.turn.keys import _api_client_address
from tai42_skeleton.operations.errors import ConflictError

from .conftest import LuaEnv, _api_route, standing_person
from .route_thread_writes import (
    IDENTITY,
    NOW,
    ROUTE,
    T_A,
    T_B,
    A,
    B,
    C,
    absorbed,
    begin,
    channel_address,
    create,
    dak,
    door,
    end_send,
    inbound,
    into,
    point,
    row_field,
    seed,
    send,
    stamps,
    threads_of,
    writers,
)

_TARGET = PairingTarget(target_kind="agent", target_name="echo")


@pytest.fixture
async def line(lua_env: LuaEnv) -> LuaEnv:
    await lua_env.manager.put_route(ROUTE)
    return lua_env


async def _map(line: LuaEnv, address: str, person_id: str | None) -> None:
    """Point the index field of ``address`` at ``person_id``, or remove it."""
    index_key = line.store.settings.person_index_key("agent", "echo")
    if person_id is None:
        await line.redis.hdel(index_key, dak(address))
    else:
        await line.redis.hset(index_key, dak(address), person_id)


# -- the record store's writes --------------------------------------------------------------------


def test_the_door_names_the_target_and_the_door_address_the_person_store_keys():
    assert json.loads(thread_door(ROUTE, T_A) or "") == ["agent", "echo", _address_key_of(channel_address(A))]
    api = _api_route("chat")
    client = _api_client_address("caller/1", "end-user-1")
    api_address = PersonAddress(door="api", address=client, routes=["chat"], linked_at=NOW)
    assert json.loads(thread_door(api, route_thread_id("chat", client)) or "") == [
        "agent",
        "echo",
        _address_key_of(api_address),
    ]
    assert thread_door(ROUTE, person_thread_id("p-1")) is None


async def test_a_create_by_an_address_nobody_holds_stamps_its_door(line):
    await create(line, inbound("in-1", T_A))

    assert await writers(line) == {door(A)}
    assert await threads_of(line, door(A)) == {T_A}


async def test_a_create_by_an_address_a_person_holds_stamps_the_person(line):
    await seed(line, "p", channel_address(A))

    await create(line, inbound("in-1", T_A))

    assert await writers(line) == {"p"}
    assert await stamps(line) == {line.store.settings.writer_threads_key("p"): {T_A}}


async def test_a_create_keeps_its_writer_on_the_row(line):
    await create(line, inbound("in-1", T_A))
    await _map(line, A, "p")
    await create(line, inbound("in-2", T_A))

    assert await row_field(line, "in-1", "writer") == door(A)
    assert await row_field(line, "in-2", "writer") == "p"


async def test_a_create_with_its_route_missing_still_stamps(line):
    await line.redis.delete(line.store.settings.route_key("line"))

    await create(line, inbound("in-1", T_A))

    assert await row_field(line, "in-1", "route_missing") == "1"
    assert await row_field(line, "in-1", "writer") == door(A)
    assert await threads_of(line, door(A)) == {T_A}


async def test_a_create_in_a_person_thread_stamps_nothing(line):
    await line.seed_person(standing_person("15550002001", "line"))

    await create(line, inbound("in-1", person_thread_id("15550002001")))

    assert await stamps(line) == {}


async def test_a_dedupe_hit_and_a_re_run_stamp_one_writer(line):
    claim = line.store.settings.dedupe_key("twilio", "PID-1")
    first = inbound("in-1", T_A, provider_message_id="PID-1")
    assert await create(line, first, claim_key=claim) == "in-1"

    assert await create(line, inbound("in-2", T_A, B, provider_message_id="PID-1"), claim_key=claim) == "in-1"
    assert await create(line, first, claim_key=claim) == "in-1"

    assert await writers(line) == {door(A)}
    assert await line.redis.exists(line.store.settings.record_key("in-2")) == 0


async def test_a_record_whose_retention_expires_and_a_thread_drop_leave_the_visit_list(line):
    await create(line, inbound("in-1", T_A))
    await create(line, inbound("in-2", T_A))

    await line.redis.delete(line.store.settings.record_key("in-1"))
    await line.store.drop_thread("line", T_A)

    assert await writers(line) == set()
    assert await threads_of(line, door(A)) == {T_A}


async def test_a_begin_stamps_its_thread_and_keeps_its_door_on_the_row(line):
    await begin(line, send("send-1", T_A))

    assert await threads_of(line, door(A)) == {T_A}
    assert await row_field(line, "send-1", "writer") == door(A)
    assert await row_field(line, "send-1", "writer_door") == door(A)


async def test_a_begin_with_the_routing_row_gone_still_stamps(line):
    await line.redis.delete(line.store.settings.route_key("line"))

    await begin(line, send("send-1", T_A))

    assert await threads_of(line, door(A)) == {T_A}


async def test_a_begin_into_a_person_thread_stamps_nothing(line):
    await line.seed_person(standing_person("15550002001", "line"))

    await begin(line, send("send-1", person_thread_id("15550002001")))

    assert await stamps(line) == {}
    assert await row_field(line, "send-1", "writer_door") in (None, "")


@pytest.mark.parametrize("how", ["complete", "end", "settle"])
async def test_an_end_landing_in_its_begin_thread_stamps_the_writer_once(line, how):
    record = send("send-1", T_A)
    await begin(line, record)

    placement = await end_send(line, how, record)

    assert placement.code == 1
    assert await writers(line) == {door(A)}
    assert await threads_of(line, door(A)) == {T_A}


async def test_a_begin_and_its_landing_keep_the_writer_on_the_row(line):
    record = send("send-1", T_A)
    await begin(line, record)
    assert await row_field(line, "send-1", "writer") == door(A)
    await _map(line, A, "p")

    await end_send(line, "end", record)

    assert await row_field(line, "send-1", "writer") == "p"


async def test_an_end_stamps_the_person_holding_the_door_when_it_lands(line):
    record = send("send-1", T_A)
    await begin(line, record)
    await seed(line, "p", channel_address(A))

    await end_send(line, "end", record)

    assert await writers(line) == {"p"}
    assert (await threads_of(line, door(A)), await threads_of(line, "p")) == ({T_A}, {T_A})


async def test_a_completion_landing_elsewhere_stamps_its_landing_thread_and_keeps_its_begin_stamp(line):
    record = send("send-1", T_A)
    await begin(line, record)
    elsewhere = record.model_copy(update={"client_address": B, "thread_id": T_B})

    placement = await end_send(line, "complete", elsewhere)

    assert placement.code == 1
    assert await writers(line, T_B) == {door(B)}
    assert await writers(line, T_A) == set()
    assert (await threads_of(line, door(A)), await threads_of(line, door(B))) == ({T_A}, {T_B})


async def test_a_second_end_stamps_nothing_more(line):
    record = send("send-1", T_A)
    await begin(line, record)
    await end_send(line, "end", record)
    await seed(line, "p", channel_address(A))
    before = await stamps(line)

    placement = await line.store.end_send_record(record, status=DeliveryStatus.FAILED, error="again")

    assert placement.code == -2
    assert await stamps(line) == before


async def test_a_send_whose_thread_was_forgotten_is_deleted_and_stamps_nothing_on_landing(line):
    record = send("send-1", T_A)
    await begin(line, record)
    await line.store.forget_sending("line", T_A)
    await seed(line, "p", channel_address(A))

    placement = await end_send(line, "end", record)

    assert (placement.code, placement.detail) == (-1, "forgotten")
    assert await line.redis.exists(line.store.settings.record_key("send-1")) == 0
    assert (await threads_of(line, door(A)), await threads_of(line, "p")) == ({T_A}, set())


async def test_a_send_landing_while_its_route_is_gone_stamps_its_landing(line):
    record = send("send-1", T_A)
    await begin(line, record)
    await line.redis.delete(line.store.settings.route_key("line"))
    await seed(line, "p", channel_address(A))

    placement = await end_send(line, "end", record)

    assert placement.code == 0
    assert await row_field(line, "send-1", "writer") == "p"
    assert (await threads_of(line, door(A)), await threads_of(line, "p")) == ({T_A}, {T_A})


# -- first contact ---------------------------------------------------------------------------------


async def test_a_created_person_adopts_every_thread_its_door_was_stamped_on(line):
    await create(line, inbound("in-1", T_A))
    await line.manager.put_route(ROUTE.model_copy(update={"route_name": "line-2"}))
    other = route_thread_id("line-2", A)
    await create(
        line, inbound("in-2", other, route_name="line-2"), route=ROUTE.model_copy(update={"route_name": "line-2"})
    )
    await create(line, inbound("in-3", T_B, B))

    person, created = await line.manager.persons.ensure_provisional(_TARGET, channel_address(A))

    assert created is True
    assert await writers(line, T_A) == {person.person_id}
    assert await writers(line, other) == {person.person_id}
    assert await threads_of(line, person.person_id) == {T_A, other}
    assert await threads_of(line, door(A)) == set()
    assert await writers(line, T_B) == {door(B)}


async def test_an_adopting_person_becomes_the_writer_of_its_doors_records(line):
    await create(line, inbound("in-1", T_A))
    await _map(line, A, "x")
    await create(line, inbound("in-2", T_A))
    await _map(line, A, None)

    person, created = await line.manager.persons.ensure_provisional(_TARGET, channel_address(A))

    assert created is True
    assert await row_field(line, "in-1", "writer") == person.person_id
    assert await row_field(line, "in-2", "writer") == "x"
    assert await writers(line) == {person.person_id, "x"}


async def test_an_existing_person_adopts_a_stamp_its_door_left_while_unheld(line):
    await seed(line, "p", channel_address(A))
    await _map(line, A, None)
    await create(line, inbound("in-1", T_A))
    await _map(line, A, "p")

    person, created = await line.manager.persons.ensure_provisional(_TARGET, channel_address(A))

    assert (person.person_id, created) == ("p", False)
    assert await writers(line) == {"p"}
    assert await stamps(line) == {line.store.settings.writer_threads_key("p"): {T_A}}


async def test_an_erasing_person_adopts_nothing(line):
    await seed(line, "p", channel_address(A))
    await _map(line, A, None)
    await create(line, inbound("in-1", T_A))
    await _map(line, A, "p")
    await line.redis.set(line.store.settings.person_erasing_key("p"), "1")

    with pytest.raises(PersonErasingError):
        await line.manager.persons.ensure_provisional(_TARGET, channel_address(A))

    assert await writers(line) == {door(A)}


# -- the link ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["p", "q"])
async def test_a_link_records_every_absorbed_id_on_the_survivor_and_leaves_the_stamps(line, first):
    await seed(line, "p", channel_address(A), age_days=2)
    await seed(line, "q", channel_address(B), age_days=1)
    await line.redis.sadd(line.store.settings.person_absorbed_key("q"), "r")
    await point(line, "r", "q")
    await create(line, inbound("in-1", T_B, B))
    before = await stamps(line)

    survivor = await line.manager.persons.merge(*(("p", "q") if first == "p" else ("q", "p")))

    assert survivor.person_id == "p"
    assert await absorbed(line, "p") == {"q", "r"}
    assert await absorbed(line, "q") == set()
    assert (await into(line, "q"), await into(line, "r")) == ("p", "p")
    assert await line.redis.exists(line.store.settings.person_key("q")) == 0
    assert await stamps(line) == before == {line.store.settings.writer_threads_key("q"): {T_B}}
    assert await writers(line, T_B) == {"q"}


@pytest.mark.parametrize(
    ("refusal", "raised"),
    [
        ("unknown", RuntimeError),
        ("cross_target", CrossTargetMergeError),
        ("duplicate_address", RuntimeError),
        ("p_marked", PersonErasingError),
        ("q_marked", PersonErasingError),
    ],
)
async def test_a_refused_link_leaves_the_absorbed_ids_and_pointers(line, refusal, raised):
    settings = line.store.settings
    q_addresses = [channel_address(B)]
    if refusal == "duplicate_address":
        q_addresses.append(channel_address(A))
    await seed(line, "p", channel_address(A), age_days=2)
    await seed(line, "q", *q_addresses, age_days=1, target_name="echo-2" if refusal == "cross_target" else "echo")
    await line.redis.sadd(settings.person_absorbed_key("q"), "r")
    await point(line, "r", "q")
    if refusal.endswith("_marked"):
        await line.redis.set(settings.person_erasing_key(refusal[0]), "1")

    with pytest.raises(raised):
        await line.manager.persons.merge("p", "nobody" if refusal == "unknown" else "q")

    assert await absorbed(line, "p") == set()
    assert await absorbed(line, "q") == {"r"}
    assert (await into(line, "q"), await into(line, "r")) == (None, "q")


async def test_the_ensure_unlink_and_locale_change_leave_every_pointer_and_stamp(line):
    persons = line.manager.persons
    await seed(line, "p", channel_address(A), age_days=2)
    await seed(line, "q", channel_address(B), channel_address(C), age_days=1)
    await create(line, inbound("in-1", T_B, B))
    await persons.merge("p", "q")
    before = (await into(line, "q"), await absorbed(line, "p"), await stamps(line))

    existing, created = await persons.ensure_provisional(_TARGET, channel_address(A, IDENTITY, "line", "line-b"))
    assert (existing.person_id, created) == ("p", False)
    await persons.set_locale("p", "en")
    await persons.set_locale("p", None)
    await persons.detach("p", door="channel", channel="twilio", our_identity=IDENTITY, address=B)

    assert (await into(line, "q"), await absorbed(line, "p"), await stamps(line)) == before


# -- the erase's read and its last step -------------------------------------------------------------


async def test_the_read_answers_every_thread_the_person_and_its_absorbed_ids_wrote_in(line):
    persons = line.manager.persons
    await seed(line, "p", channel_address(A), age_days=2)
    await seed(line, "q", channel_address(B), age_days=1)
    await create(line, inbound("in-1", T_A))
    await create(line, inbound("in-2", T_B, B))
    third = route_thread_id("line", C)
    await create(line, inbound("in-3", third, C))
    await persons.merge("p", "q")

    read = await persons.read_for_erase("p")

    assert read.absorbed_ids == ["q"]
    assert read.route_threads == sorted([T_A, T_B])


async def test_the_read_of_a_gone_person_answers_no_row_and_nothing_absorbed(line):
    read = await line.manager.persons.read_for_erase("nobody")

    assert (read.person, read.absorbed_ids, read.absorbed_into, read.route_threads) == (None, [], None, [])
    assert read.thread_routes == {"nobody": []}


async def test_the_erase_removes_the_visit_lists_of_its_ids_and_leaves_every_records_writer(line):
    # Nothing cleared T_A or T_B here, and no writer outside keeps either (x has no row), so each
    # goes to the visit list of the door its erased records were written through.
    persons = line.manager.persons
    await seed(line, "p", channel_address(A), age_days=2)
    await seed(line, "q", channel_address(B), age_days=1)
    await create(line, inbound("in-1", T_A))
    await create(line, inbound("in-2", T_B, B))
    await persons.merge("p", "q")
    await _map(line, B, "x")
    await create(line, inbound("in-3", T_B, B))
    await _map(line, B, "p")
    assert await persons.mark_erasing("p") is True
    read = await persons.read_for_erase("p")
    assert read.person is not None

    await persons.erase(read.person, read.absorbed_ids, read.route_threads)

    door_a, door_b = thread_door(ROUTE, T_A) or "", thread_door(ROUTE, T_B) or ""
    assert await stamps(line) == {
        line.store.settings.writer_threads_key("x"): {T_B},
        line.store.settings.writer_threads_key(door_a): {T_A},
        line.store.settings.writer_threads_key(door_b): {T_B},
    }
    assert [await row_field(line, mid, "writer") for mid in ("in-1", "in-2", "in-3")] == ["p", "q", "x"]
    assert (await absorbed(line, "p"), await into(line, "q")) == (set(), None)
    assert await line.redis.exists(line.store.settings.person_key("p")) == 0
    assert await line.redis.exists(line.store.settings.person_erasing_key("p")) == 0


async def test_the_erase_refuses_a_thread_its_ids_wrote_in_after_its_read_with_nothing_removed(line):
    persons = line.manager.persons
    await seed(line, "p", channel_address(A), age_days=2)
    await create(line, inbound("in-1", T_A))
    assert await persons.mark_erasing("p") is True
    read = await persons.read_for_erase("p")
    assert read.person is not None
    await _map(line, B, "p")
    await create(line, inbound("in-2", T_B, B))
    before = await stamps(line)

    with pytest.raises(ConflictError, match=re.escape(f"a write by 'p' in the thread {T_B!r} landed")):
        await persons.erase(read.person, read.absorbed_ids, read.route_threads)

    assert await stamps(line) == before
    assert await line.redis.exists(line.store.settings.person_key("p")) == 1
    assert await line.redis.exists(line.store.settings.person_erasing_key("p")) == 1


# -- a person thread's recorded routes ------------------------------------------------------------


async def _recorded(line: LuaEnv, person_id: str) -> set[str]:
    return await line.redis.smembers(line.store.settings.person_thread_routes_key(person_id))


async def test_the_recorded_routes_are_read_with_the_addresses_and_left_by_every_step_but_the_erase(line):
    persons, settings = line.manager.persons, line.store.settings
    await seed(line, "p", channel_address(A), channel_address(C), age_days=2)
    await seed(line, "q", channel_address(B), age_days=1)
    await line.redis.sadd(settings.person_thread_routes_key("p"), "line-x")
    await line.redis.sadd(settings.person_thread_routes_key("q"), "line-y")
    await persons.merge("p", "q")
    assert (await _recorded(line, "p"), await _recorded(line, "q")) == ({"line-x"}, {"line-y"})

    read = await persons.read_for_erase("p")

    assert read.thread_routes == {"p": ["line", "line-x"], "q": ["line", "line-y"]}
    await persons.set_locale("p", "en")
    await persons.detach("p", door="channel", channel="twilio", our_identity=IDENTITY, address=C)
    await line.store.drop_thread("line", person_thread_id("p"))
    assert (await _recorded(line, "p"), await _recorded(line, "q")) == ({"line-x"}, {"line-y"})
    assert await persons.mark_erasing("p") is True
    read = await persons.read_for_erase("p")
    assert read.person is not None
    await persons.erase(read.person, read.absorbed_ids, read.route_threads)
    assert (await _recorded(line, "p"), await _recorded(line, "q")) == (set(), set())
    gone = await persons.read_for_erase("p")
    assert (gone.person, gone.absorbed_ids, gone.thread_routes) == (None, [], {"p": []})
