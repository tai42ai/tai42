"""A person erase against person-subject writes on a REAL Postgres: the gate barrier and the drain's replay.

OPT-IN like every real-Postgres states test: set ``TAI42_SKELETON_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres; without it the tests skip visibly. The persons
run the real script text on ``fakeredis[lua]``; the erase is stopped at a chosen step by a pause.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.conversations import TargetConversationConfig
from tai42_contract.states.errors import SubjectRefusedError
from tai42_contract.states.models import (
    AttachBody,
    StateBatchWrite,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    WriteOrigin,
)
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.db import apply_migrations
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.app.facets import StatesFacet
from tai42_skeleton.conversations.persons import PersonErasingError
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.states import db as states_db
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.db import states_entry
from tai42_skeleton.states.outbox import drain as drain_mod
from tai42_skeleton.states.outbox import enqueue as enqueue_mod
from tai42_skeleton.states.outbox import sweep as sweep_mod
from tai42_skeleton.states.service import StatesService

from ..states.fake_service_store import _FakeStorage
from ..states.outbox.conftest import SCHEMA, execute, require_real_pg
from .conftest import LuaEnv, _channel_route, pause_call, standing_person

pytestmark = pytest.mark.integration

_PERSON = "15550002222"
_ORIGIN = WriteOrigin(consumer="turn")


def _counter(name: str) -> StateTemplateDocument:
    return StateTemplateDocument.model_validate(
        {
            "name": name,
            "schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
            "template_jq": {
                "bump": {
                    "purpose": "update",
                    "writes": [["count"]],
                    "jq": {"content": '[{op: "set", path: ["count"], value: ((.count // 0) + 1)}]'},
                },
            },
        }
    )


@pytest.fixture
async def states(monkeypatch) -> AsyncIterator[tuple[StatesService, str]]:
    """A ``person``-and-``thread`` state on a real database behind a bound app; saves stay held until drained."""
    require_real_pg()
    reset_all_settings()
    await apply_migrations([states_entry()])
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    monkeypatch.setattr(states_db, "states_store_configured", lambda: True)
    svc = StatesService()
    monkeypatch.setattr(drain_mod, "live_states_service", lambda: svc)
    monkeypatch.setattr(sweep_mod, "live_states_service", lambda: svc)

    async def _hold(service: Any, row_id: int, *, has_records: bool, has_calls: bool) -> None:
        return None

    monkeypatch.setattr(enqueue_mod, "dispatch_pending_save", _hold)
    app = SimpleNamespace(_states_service=svc, storage=_FakeStorage())
    app.states = StatesFacet(app)  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(instance, "app", app, raising=False)
    state = f"st_{uuid.uuid4().hex[:12]}"
    template = f"tp-{uuid.uuid4().hex[:12]}"
    with tai42_app.bound(app):
        await svc.put_declaration(
            StateDeclaration(
                name=state, schema=SCHEMA, subject_kinds=["thread", "person"], default_subject_kind="thread"
            )
        )
        await svc.put_template(_counter(template), replace=False)
        await svc.attach(state, template, AttachBody(path=[]))
        try:
            yield svc, state
        finally:
            await svc.detach(state, template)
            await execute("DELETE FROM state_outbox WHERE states @> ARRAY[%s]::text[]", (state,))
            await execute("DELETE FROM state_writes WHERE state = %s", (state,))
            await execute("DELETE FROM state_records WHERE state = %s", (state,))
            await execute("DELETE FROM state_declarations WHERE name = %s", (state,))
            await svc.delete_template(template)
            await shutdown_all_clients()


def _subject() -> StateSubject:
    return StateSubject(target_kind="agent", target_name="echo", kind="person", key=_PERSON)


async def _standing(lua_env: LuaEnv) -> None:
    await lua_env.manager.put_route(_channel_route())
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    )
    await lua_env.seed_person(standing_person(_PERSON, "line", target_name="echo"))


async def _person_rows(state: str) -> list[tuple[Any, ...]]:
    return await execute(
        "SELECT data FROM state_records WHERE state = %s AND subject_kind = 'person' AND subject_key = %s",
        (state, _PERSON),
    )


async def _outbox_rows(state: str) -> list[tuple[Any, ...]]:
    return await execute("SELECT id FROM state_outbox WHERE states @> ARRAY[%s]::text[]", (state,))


async def test_a_write_admitted_before_the_erase_settles_its_gate_commits_first_and_is_then_erased(
    states, lua_env, checkpoint_saver, monkeypatch
):
    svc, state = states
    await _standing(lua_env)
    order: list[str] = []
    admitted, release = asyncio.Event(), asyncio.Event()
    real_admit = svc._admit_person
    real_settle = svc._store.settle_person_writes

    async def _held_admit(subject, *, admit_erasing=False):
        await real_admit(subject, admit_erasing=admit_erasing)
        if not admitted.is_set():
            admitted.set()
            await release.wait()

    async def _settle_spy(subject, *, timeout_seconds):
        order.append("settling")
        await real_settle(subject, timeout_seconds=timeout_seconds)
        order.append("settled")

    monkeypatch.setattr(svc, "_admit_person", _held_admit)
    monkeypatch.setattr(svc._store, "settle_person_writes", _settle_spy)
    write = asyncio.ensure_future(svc.replace(state, _subject(), {"n": 5}, origin=_ORIGIN))
    await asyncio.wait_for(admitted.wait(), 10)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    async with asyncio.timeout(10):
        while "settling" not in order:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.5)

    assert order == ["settling"]
    assert not erase.done()
    release.set()
    assert (await asyncio.wait_for(write, 30)).data == {"n": 5}
    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert order == ["settling", "settled"]
    assert await _person_rows(state) == []


async def test_a_pending_save_inserted_before_the_mark_is_applied_by_the_erase_drain_and_cleared(
    states, lua_env, checkpoint_saver
):
    svc, state = states
    await _standing(lua_env)
    await _stage(svc, state, "ops")
    assert len(await _outbox_rows(state)) == 1

    assert (await ops.delete_conversation_person(_PERSON))["erased"] is True

    assert await _person_rows(state) == []
    assert await _outbox_rows(state) == []


async def _stage(svc: StatesService, state: str, kind: str) -> None:
    async with svc.open_unit() as unit:
        if kind == "replace":
            await unit.stage_replace(state, _subject(), {"n": 7}, _ORIGIN)
        elif kind == "ops":
            await unit.stage(
                [
                    StateBatchWrite(
                        state=state, subject=_subject(), ops=[{"op": "set", "path": ["n"], "value": 7}], origin=_ORIGIN
                    )
                ]
            )
        else:
            await unit.stage([StateBatchWrite(state=state, subject=_subject(), template_jq="bump", origin=_ORIGIN)])
        await unit.commit()


@pytest.mark.parametrize("kind", ["replace", "ops", "template_jq"])
async def test_a_pending_save_whose_base_moved_is_replayed_by_the_erase_drain_and_erased(
    kind, states, lua_env, checkpoint_saver, monkeypatch
):
    svc, state = states
    await _standing(lua_env)
    await svc.replace(state, _subject(), {"n": 1}, origin=_ORIGIN)
    await _stage(svc, state, kind)
    await execute(
        "UPDATE state_records SET updated_at = updated_at + interval '1 second' WHERE state = %s AND subject_key = %s",
        (state, _PERSON),
    )
    replays: list[str] = []
    real_replay = svc._replay_subject

    async def _replay_spy(*args, **kwargs):
        replays.append(kind)
        return await real_replay(*args, **kwargs)

    monkeypatch.setattr(svc, "_replay_subject", _replay_spy)
    marked = pause_call(monkeypatch, lua_env.manager.persons, "mark_erasing", after=True)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    await asyncio.wait_for(marked.reached.wait(), 10)
    marked.go.set()

    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert replays == [kind]
    assert await _person_rows(state) == []
    assert await _outbox_rows(state) == []
    assert await lua_env.redis.exists(lua_env.store.settings.person_erasing_key(_PERSON)) == 0


async def test_a_pending_save_on_a_person_with_no_row_is_still_refused_at_its_replay(states, lua_env, caplog):
    svc, state = states
    await _standing(lua_env)
    await svc.replace(state, _subject(), {"n": 1}, origin=_ORIGIN)
    await _stage(svc, state, "ops")
    await execute(
        "UPDATE state_records SET updated_at = updated_at + interval '1 second' WHERE state = %s AND subject_key = %s",
        (state, _PERSON),
    )
    await lua_env.redis.delete(lua_env.store.settings.person_key(_PERSON))

    unsettled = await svc.drain_target_saves("agent", "echo")

    assert len(unsettled) == 1
    assert "names no person in the identity store" in caplog.text
    assert len(await _outbox_rows(state)) == 1


def _aliased_thread() -> StateSubject:
    return StateSubject(target_kind="agent", target_name="echo", kind="thread", key="thread-folded")


async def _thread_folded_into_the_person(svc: StatesService, state: str) -> None:
    await svc.replace(state, _subject(), {"n": 1}, origin=_ORIGIN)
    await svc.fold(state, _aliased_thread(), _subject(), "switch", origin=_ORIGIN)


async def _write_on(svc: StatesService, state: str, kind: str) -> Any:
    if kind == "replace":
        return await svc.replace(state, _aliased_thread(), {"n": 9}, origin=_ORIGIN)
    ops_ = [{"op": "set", "path": ["n"], "value": 9}]
    return await svc.apply(state, _aliased_thread(), ops_, op_id=None, origin=_ORIGIN)


@pytest.mark.parametrize("kind", ["replace", "ops"])
async def test_a_write_through_an_alias_resolved_before_the_erase_is_refused_after_it(
    kind, states, lua_env, checkpoint_saver, monkeypatch
):
    svc, state = states
    await _standing(lua_env)
    await _thread_folded_into_the_person(svc, state)
    resolved, release = asyncio.Event(), asyncio.Event()
    real_resolve = svc._store._resolve_subject

    async def _held_resolve(cur, state_name, subject, *, check_outbox):
        canonical = await real_resolve(cur, state_name, subject, check_outbox=check_outbox)
        if subject == _aliased_thread() and not resolved.is_set():
            resolved.set()
            await release.wait()
        return canonical

    monkeypatch.setattr(svc._store, "_resolve_subject", _held_resolve)
    write = asyncio.ensure_future(_write_on(svc, state, kind))
    await asyncio.wait_for(resolved.wait(), 10)

    assert (await asyncio.wait_for(ops.delete_conversation_person(_PERSON), 30))["erased"] is True
    release.set()
    with pytest.raises((SubjectRefusedError, PersonErasingError)):
        await asyncio.wait_for(write, 30)
    assert await _person_rows(state) == []


async def test_a_write_through_an_alias_admitted_before_the_erase_holds_the_erase_until_it_commits(
    states, lua_env, checkpoint_saver, monkeypatch
):
    svc, state = states
    await _standing(lua_env)
    await _thread_folded_into_the_person(svc, state)
    order: list[str] = []
    admitted, release = asyncio.Event(), asyncio.Event()
    real_admit = svc._admit_person
    real_settle = svc._store.settle_person_writes

    async def _held_admit(subject, *, admit_erasing=False):
        await real_admit(subject, admit_erasing=admit_erasing)
        if not admitted.is_set():
            admitted.set()
            await release.wait()

    async def _settle_spy(subject, *, timeout_seconds):
        order.append("settling")
        await real_settle(subject, timeout_seconds=timeout_seconds)
        order.append("settled")

    monkeypatch.setattr(svc, "_admit_person", _held_admit)
    monkeypatch.setattr(svc._store, "settle_person_writes", _settle_spy)
    write = asyncio.ensure_future(_write_on(svc, state, "replace"))
    await asyncio.wait_for(admitted.wait(), 10)
    erase = asyncio.ensure_future(ops.delete_conversation_person(_PERSON))
    async with asyncio.timeout(10):
        while "settling" not in order:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.5)

    assert order == ["settling"]
    assert not erase.done()
    release.set()
    assert (await asyncio.wait_for(write, 30)).data == {"n": 9}
    assert (await asyncio.wait_for(erase, 30))["erased"] is True
    assert await _person_rows(state) == []
