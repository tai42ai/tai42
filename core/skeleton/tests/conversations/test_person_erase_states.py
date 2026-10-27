"""A person's declared-state memory under its erase mark: every write on it is refused, and none outlives the erase.

The persons run the real script text on ``fakeredis[lua]``; the states service is the real one
over the in-memory fake Postgres. The erase is stopped at a chosen step by a pause on one call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.conversations import PersonAddress, TargetConversationConfig
from tai42_contract.errors import UnavailableError
from tai42_contract.states.errors import SubjectRefusedError
from tai42_contract.states.models import StateBatchWrite, StateSubject, WriteOrigin

import tai42_skeleton.states.store as states_store_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.facets import StatesFacet
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.persons import PersonErasingError
from tai42_skeleton.conversations.thread_ids import person_thread_id
from tai42_skeleton.interactions import helper as interactions_helper
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.conversations import threads_delete as threads_delete_module
from tai42_skeleton.operations.errors import ConflictError
from tai42_skeleton.states.outbox import enqueue as enqueue_module
from tai42_skeleton.states.service import StatesService

from ..states.conftest import FakeStatesPg, _FakePool
from ..states.fake_service_store import _FakeStorage
from .conftest import LuaEnv, _channel_route, create_record, pause_call, standing_person

_STATE = "assistant_memory"
_PERSON = "15550002222"
_OTHER = "15550003333"
_ORIGIN = WriteOrigin(consumer="turn")


@pytest.fixture
def states_pg(monkeypatch) -> Iterator[FakeStatesPg]:
    """The states store on an in-memory fake Postgres, configured, behind a bound app's ``states`` facet."""
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeStatesPg()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, **kwargs):
        yield _FakePool(fake)

    monkeypatch.setattr(states_store_module, "client_ctx", _ctx)

    async def _no_dispatch(service, row_id, *, has_records, has_calls):
        return None

    monkeypatch.setattr(enqueue_module, "dispatch_pending_save", _no_dispatch)
    app = SimpleNamespace(_states_service=StatesService(), storage=_FakeStorage())
    app.states = StatesFacet(app)  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(instance, "app", app, raising=False)
    fake.seed_declaration(_STATE, subject_kinds=["thread", "person"])
    with tai42_app.bound(app):
        yield fake


def _service() -> StatesService:
    return instance.app._states_service


def _subject(key: str) -> StateSubject:
    return StateSubject(target_kind="agent", target_name="echo", kind="person", key=key)


def _row(key: str) -> tuple[str, str, str, str, str]:
    return (_STATE, "agent", "echo", "person", key)


async def _standing(lua_env: LuaEnv, states_pg: FakeStatesPg, *person_ids: str) -> None:
    """Each id a person of the agent ``echo`` on route ``line``, with a ``person``-subject state."""
    await lua_env.manager.put_route(_channel_route())
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    )
    for person_id in person_ids:
        await lua_env.seed_person(standing_person(person_id, "line", target_name="echo"))
        states_pg.seed_record(_STATE, "agent", "echo", "person", person_id, {"n": 1})


def _restore_row(key: str) -> dict[str, Any]:
    return {"target_kind": "agent", "target_name": "echo", "subject_kind": "person", "subject_key": key, "data": {}}


def _alias_row(alias: str, canonical: str) -> dict[str, Any]:
    return {
        "target_kind": "agent",
        "target_name": "echo",
        "alias_kind": "person",
        "alias_key": alias,
        "canonical_kind": "person",
        "canonical_key": canonical,
        "mode": "switch",
    }


async def _write(kind: str) -> None:
    svc = _service()
    if kind == "replace":
        await svc.replace(_STATE, _subject(_PERSON), {"n": 2}, origin=_ORIGIN)
    elif kind == "apply":
        await svc.apply(
            _STATE, _subject(_PERSON), [{"op": "set", "path": ["n"], "value": 2}], op_id=None, origin=_ORIGIN
        )
    elif kind == "apply_batch":
        await svc.apply_batch(
            [
                StateBatchWrite(
                    state=_STATE,
                    subject=_subject(_PERSON),
                    ops=[{"op": "set", "path": ["n"], "value": 2}],
                    origin=_ORIGIN,
                )
            ]
        )
    elif kind == "fold_subject":
        await svc.fold(_STATE, _subject(_PERSON), _subject(_OTHER), "switch", origin=_ORIGIN)
    elif kind == "fold_into":
        await svc.fold(_STATE, _subject(_OTHER), _subject(_PERSON), "switch", origin=_ORIGIN)
    else:
        await svc.restore_records(_STATE, [_restore_row(_PERSON)], origin=_ORIGIN)


@pytest.mark.parametrize("kind", ["replace", "apply", "apply_batch", "fold_subject", "fold_into", "restore"])
async def test_a_state_write_on_the_person_after_its_state_erase_is_refused_and_leaves_no_row(
    kind, lua_env, states_pg, monkeypatch, checkpoint_saver
):
    await _standing(lua_env, states_pg, _PERSON, _OTHER)
    cleared = pause_call(monkeypatch, threads_delete_module, "_erase_person_state", after=True)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await cleared.reached.wait()

    with pytest.raises(PersonErasingError):
        await _write(kind)
    cleared.go.set()

    assert (await erase)["erased"] is True
    assert _row(_PERSON) not in states_pg.records
    assert states_pg.records[_row(_OTHER)]["data"] == {"n": 1}


async def test_a_unit_staged_before_the_mark_and_committed_after_it_writes_no_pending_save(
    lua_env, states_pg, monkeypatch, checkpoint_saver
):
    await _standing(lua_env, states_pg, _PERSON)
    marked = pause_call(monkeypatch, lua_env.manager.persons, "mark_erasing", after=True)
    async with _service().open_unit() as unit:
        await unit.stage(
            [
                StateBatchWrite(
                    state=_STATE,
                    subject=_subject(_PERSON),
                    ops=[{"op": "set", "path": ["n"], "value": 2}],
                    origin=_ORIGIN,
                )
            ]
        )
        erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
        await marked.reached.wait()

        with pytest.raises(PersonErasingError):
            await unit.commit()

    assert states_pg.outbox == {}
    marked.go.set()
    assert (await erase)["erased"] is True
    assert _row(_PERSON) not in states_pg.records


async def test_an_alias_restore_naming_a_marked_person_writes_no_alias(lua_env, states_pg):
    await _standing(lua_env, states_pg, _PERSON)
    assert await lua_env.manager.persons.mark_erasing(_PERSON) is True

    for row in (_alias_row(_PERSON, "someone"), _alias_row("someone", _PERSON)):
        with pytest.raises(PersonErasingError):
            await _service().restore_aliases(_STATE, [row], origin=_ORIGIN)

    assert states_pg.aliases == {}


async def test_an_alias_restore_naming_a_person_with_no_row_restores(lua_env, states_pg):
    await _service().restore_aliases(_STATE, [_alias_row("ghost", "someone")], origin=_ORIGIN)

    assert list(states_pg.aliases) == [(_STATE, "agent", "echo", "person", "ghost")]


async def test_a_state_write_on_a_person_with_no_row_and_no_mark_is_still_refused_as_unknown(lua_env, states_pg):
    with pytest.raises(SubjectRefusedError, match="names no person"):
        await _service().replace(_STATE, _subject("ghost"), {"n": 1}, origin=_ORIGIN)


def _p_address(our_identity: str, route: str) -> PersonAddress:
    return PersonAddress(
        door="channel",
        routes=[route],
        channel="twilio",
        our_identity=our_identity,
        address=f"+{_PERSON}",
        linked_at=datetime.now(UTC),
    )


async def _person_thread_record(lua_env: LuaEnv, message_id: str, route) -> None:
    now = time.time()
    await create_record(
        lua_env.store,
        ConversationRecord(
            message_id=message_id,
            route_name=route.route_name,
            door="channel",
            thread_id=person_thread_id(_PERSON),
            client_address=f"+{_PERSON}",
            channel="twilio",
            our_identity=route.our_identity,
            origin="client",
            inbound_text="hi",
            answer_status="answered",
            answer="ok",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        ),
        route=route,
    )


@pytest.mark.parametrize("drain_fails", [False, True])
async def test_an_erase_of_a_person_a_link_absorbs_before_its_mark_is_refused_and_the_survivor_erase_clears_it(
    drain_fails, lua_env, states_pg, monkeypatch, checkpoint_saver
):
    line, line_b = _channel_route(), _channel_route("line-b", "+15550009999")
    for route in (line, line_b):
        await lua_env.manager.put_route(route)
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    )
    person = standing_person(_PERSON, "line", target_name="echo").model_copy(
        update={"addresses": [_p_address("+15550001111", "line"), _p_address("+15550009999", "line-b")]}
    )
    await lua_env.seed_person(person)
    survivor = standing_person(_OTHER, "line", target_name="echo").model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(days=1)}
    )
    await lua_env.seed_person(survivor)
    await _person_thread_record(lua_env, "p-line", line)
    await _person_thread_record(lua_env, "p-line-b", line_b)
    states_pg.seed_record(_STATE, "agent", "echo", "person", _PERSON, {"n": 1})
    states_pg.seed_record(_STATE, "agent", "echo", "thread", person_thread_id(_PERSON), {"n": 1})
    parks_cancelled: list[str] = []

    async def _cancel_spy(thread_id, **kwargs):
        parks_cancelled.append(thread_id)

    monkeypatch.setattr(interactions_helper, "cancel_parks_for_thread", _cancel_spy)
    svc = _service()
    real_drain = svc.drain_target_saves
    drains: list[str] = []
    failing = [drain_fails]

    async def _drain(target_kind, target_name):
        drains.append(target_name)
        if failing[0]:
            raise UnavailableError("the pending saves are unreachable")
        return await real_drain(target_kind, target_name)

    monkeypatch.setattr(svc, "drain_target_saves", _drain)
    persons = lua_env.manager.persons
    marking = pause_call(monkeypatch, persons, "mark_erasing")
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await marking.reached.wait()
    merged = await persons.merge(_PERSON, _OTHER)
    assert merged.person_id == _OTHER
    index = await lua_env.redis.hgetall(lua_env.store.settings.keyspace.person_index_key("agent", "echo"))
    records = sorted(states_pg.records)
    marking.go.set()

    with pytest.raises(ConflictError) as refused:
        await erase

    assert str(refused.value) == (
        f"conversation person {_PERSON!r} was absorbed into person {_OTHER!r} by a link and is not erased on its "
        f"own: erase {_OTHER!r}, which clears {_OTHER!r} and every person it absorbed, {_PERSON!r} included"
    )
    assert refused.value.extra == {"absorbed_into_person_id": _OTHER}
    settings = lua_env.store.settings
    assert await lua_env.redis.exists(settings.keyspace.person_erasing_key(_PERSON)) == 0
    assert parks_cancelled == []
    assert checkpoint_saver.deleted == []
    assert drains == []
    assert await persons.get_by_id(_OTHER) == merged
    assert await lua_env.redis.hgetall(settings.keyspace.person_index_key("agent", "echo")) == index
    assert sorted(states_pg.records) == records
    for message_id in ("p-line", "p-line-b"):
        assert await lua_env.store.get_record(message_id) is not None
    read = await persons.read_for_erase(_OTHER)
    assert read.thread_routes[_PERSON] == ["line", "line-b"]
    failing[0] = False

    result = await ops.delete_conversation_person(_OTHER)

    assert result["erased"] is True
    assert result["absorbed_person_ids"] == [_PERSON]
    for route in ("line", "line-b"):
        assert await lua_env.redis.exists(settings.keyspace.thread_index_key(route, person_thread_id(_PERSON))) == 0
        assert await lua_env.redis.zscore(settings.keyspace.route_threads_key(route), person_thread_id(_PERSON)) is None
    for message_id in ("p-line", "p-line-b"):
        assert await lua_env.store.get_record(message_id) is None
    assert _row(_PERSON) not in states_pg.records
    assert (_STATE, "agent", "echo", "thread", person_thread_id(_PERSON)) not in states_pg.records
