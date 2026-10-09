"""The commit as one pending save: what the ``state_outbox`` row carries, and what enqueues nothing.

Driven on the faithful in-memory Postgres with the save's dispatch held back, so the enqueued
row is observed before anything applies it. The deferred-call kind ``probe`` is a neutral
synthetic consumer registered under the ``tool`` kind name the unit's ``defer_call`` uses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states import StateAttach, StateBinding, StateUpdate
from tai42_contract.states.models import (
    AttachBody,
    StateBatchWrite,
    StateContext,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    SubjectCandidates,
    WriteOrigin,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.context import state_context
from tai42_skeleton.states.outbox import calls as calls_mod
from tai42_skeleton.states.outbox import enqueue as enqueue_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.service.unit import current_state_unit
from tai42_skeleton.states.store import PostgresStatesStore
from tai42_skeleton.tools.state_binding import apply_binding_updates

from .conftest import FakeStatesPg
from .fake_service_store import _FakeApp

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}, "note": {"type": "string"}}}


def _subject(key: str = "t1") -> StateSubject:
    return StateSubject(target_kind="agent", target_name="a", kind="thread", key=key)


def _write(subject: StateSubject, ops: list[dict[str, Any]], *, run_id: str | None = None) -> StateBatchWrite:
    return StateBatchWrite(state="notes", subject=subject, ops=ops, origin=WriteOrigin(consumer="c", run_id=run_id))


class _ProbeKind:
    """A neutral deferred-call kind: captures its arguments, records every apply."""

    def __init__(self) -> None:
        self.applied: list[tuple[dict[str, Any], str]] = []

    async def capture(self, target: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"target": target, "arguments": arguments}

    async def apply(self, payload: dict[str, Any], *, idempotency_key: str) -> None:
        self.applied.append((payload, idempotency_key))

    async def resumable(self, payload: dict[str, Any]) -> bool:
        return False


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _ProbeKind:
    kind = _ProbeKind()
    monkeypatch.setattr(calls_mod, "_kinds", {})
    calls_mod.register_deferred_call_kind("tool", kind)
    return kind


@pytest.fixture
def held_back(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool, bool]]:
    """Hold every enqueued save back from its apply; record what would have been dispatched."""
    dispatched: list[tuple[int, bool, bool]] = []

    async def _hold(service: Any, row_id: int, *, has_records: bool, has_calls: bool) -> None:
        dispatched.append((row_id, has_records, has_calls))

    monkeypatch.setattr(enqueue_mod, "dispatch_pending_save", _hold)
    return dispatched


@pytest.fixture
def svc(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=PostgresStatesStore())


def _decl() -> StateDeclaration:
    return StateDeclaration(name="notes", schema=_SCHEMA, subject_kinds=["thread"], default_subject_kind="thread")


async def test_the_row_carries_every_item_subject_key_and_origin(
    svc: StatesService, pg: FakeStatesPg, held_back: list
) -> None:
    await svc.put_declaration(_decl())
    pg.aliases[("notes", "agent", "a", "thread", "old")] = {
        "state": "notes",
        "target_kind": "agent",
        "target_name": "a",
        "alias_kind": "thread",
        "alias_key": "old",
        "canonical_kind": "thread",
        "canonical_key": "t1",
        "mode": "switch",
    }
    pg.seed_record("notes", "agent", "a", "thread", "t1", {"n": 1})
    ctx = StateContext(
        door="conversation",
        candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "t1", "person": "p-1"}),
        actor="alice",
        turn_id="turn-1",
    )
    with state_context(ctx):
        async with svc.open_unit() as unit:
            await unit.stage([_write(_subject(), [{"op": "set", "path": ["n"], "value": 2}], run_id="run-1")])
            await unit.stage_replace("notes", _subject("t2"), {"note": "x"}, WriteOrigin(consumer="c"))
            result = await unit.commit()
    assert result.outbox_id == "1"
    assert held_back == [(1, True, False)]
    row = pg.outbox[1]
    assert row["status"] == "pending"
    assert row["run_id"] == "run-1"
    assert row["states"] == ["notes"]
    assert set(row["record_keys"]) == {
        '["notes","agent","a","thread","t1"]',
        '["notes","agent","a","thread","old"]',
        '["notes","agent","a","thread","t2"]',
    }
    assert set(row["subject_keys"]) == {
        '["agent","a","thread","t1"]',
        '["agent","a","thread","old"]',
        '["agent","a","thread","t2"]',
        '["agent","a","person","p-1"]',
    }
    assert row["targets"] == ['["agent","a"]']
    first, second = row["records"]
    assert first["kind"] == "batch"
    assert first["write"]["ops"] == [{"op": "set", "path": ["n"], "value": 2}]
    assert first["completed_origin"]["door"] == "conversation"
    assert first["completed_origin"]["actor"] == "alice"
    assert first["completed_origin"]["turn_id"] == "turn-1"
    assert first["paths"] == [["n"]]
    assert first["provisional"]["data"] == {"n": 2}
    assert second["kind"] == "replace"
    assert second["data"] == {"note": "x"}
    subjects = {s["subject"]["key"]: s for s in row["subjects"]}
    assert subjects["t1"]["aliases"] == [{"target_kind": "agent", "target_name": "a", "kind": "thread", "key": "old"}]
    assert subjects["t1"]["projected"] == {"n": 2}
    assert subjects["t1"]["base_seq"] is not None
    assert subjects["t2"]["base_seq"] is None
    assert subjects["t2"]["projected"] == {"note": "x"}
    assert row["calls"] == []
    assert not pg.writes  # nothing applied yet


async def test_an_empty_unit_enqueues_nothing(svc: StatesService, pg: FakeStatesPg, held_back: list) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        result = await unit.commit()
    assert result.outbox_id is None
    assert result.results == []
    assert not pg.outbox
    assert held_back == []


async def test_a_calls_only_row_starts_in_calls_and_takes_the_defer_call_run_id(
    svc: StatesService, pg: FakeStatesPg, held_back: list, probe: _ProbeKind
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.defer_call("echo", {"x": 1}, run_id="run-7")
        result = await unit.commit()
    assert result.deferred_calls == 1
    row = pg.outbox[int(result.outbox_id or 0)]
    assert row["status"] == "calls"
    assert row["records_applied_at"] is not None
    assert row["run_id"] == "run-7"
    assert row["calls"] == [
        {"kind": "tool", "target": "echo", "payload": {"target": "echo", "arguments": {"x": 1}}, "run_id": "run-7"}
    ]
    assert held_back == [(1, False, True)]


async def test_a_savepoint_rollback_drops_the_calls_staged_inside_it(
    svc: StatesService, pg: FakeStatesPg, held_back: list, probe: _ProbeKind
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.defer_call("kept", {})

        async def _failing_child() -> None:
            async with unit.savepoint():
                await unit.defer_call("dropped", {})
                raise RuntimeError("child")

        with pytest.raises(RuntimeError, match="child"):
            await _failing_child()
        result = await unit.commit()
    assert [c["target"] for c in pg.outbox[int(result.outbox_id or 0)]["calls"]] == ["kept"]


async def test_a_discard_drops_the_calls(
    svc: StatesService, pg: FakeStatesPg, held_back: list, probe: _ProbeKind
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.defer_call("echo", {})
        await unit.discard()
    assert not pg.outbox


def _binding_app(svc: StatesService) -> Any:
    class _RenderRM:
        async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
            assert text.content is not None
            return text.content

    return SimpleNamespace(states=svc, storage=SimpleNamespace(resource_manager=_RenderRM()))


_SUBJECT_JQ = '{target_kind: "agent", target_name: "a", kind: "thread", key: "t1"}'


async def test_door_binding_updates_with_no_unit_open_enqueue_one_pending_save(
    svc: StatesService, pg: FakeStatesPg, held_back: list
) -> None:
    await svc.put_declaration(_decl())
    binding = StateBinding(
        states=[
            StateAttach(
                state="notes",
                subject_expr=TemplatedText(content=_SUBJECT_JQ),
                updates=[StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["n"], value: 7}]'))],
            )
        ]
    )
    await apply_binding_updates(_binding_app(svc), binding, {}, {}, door_id="d1")
    assert not pg.records  # enqueued, not applied
    assert [r["write"]["ops"] for r in pg.outbox[1]["records"]] == [[{"op": "set", "path": ["n"], "value": 7}]]
    assert current_state_unit() is None


_COUNTER_TEMPLATE = StateTemplateDocument.model_validate(
    {
        "name": "counter",
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


async def test_a_two_item_batch_on_one_subject_enqueues_item_twos_ops_over_item_ones_projection(
    svc: StatesService, pg: FakeStatesPg, held_back: list
) -> None:
    await svc.put_declaration(
        StateDeclaration(name="notes", schema=_SCHEMA, subject_kinds=["thread"], default_subject_kind="thread")
    )
    await svc.put_template(_COUNTER_TEMPLATE, replace=False)
    await svc.attach("notes", "counter", AttachBody(path=[]))
    bump = StateBatchWrite(state="notes", subject=_subject(), template_jq="bump", origin=WriteOrigin(consumer="c"))
    result = await svc.enqueue_batch([bump, bump])
    assert [r.data for r in result.results] == [{"count": 1}, {"count": 2}]
    row = pg.outbox[int(result.outbox_id or 0)]
    # Item 2's update program read item 1's projection, not the committed (absent) record.
    assert row["records"][1]["applied_ops"] == [{"op": "set", "path": ["count"], "value": 2}]
    assert row["subjects"][0]["projected"] == {"count": 2}
    assert current_state_unit() is None


async def test_enqueue_batch_of_nothing_enqueues_nothing(svc: StatesService, pg: FakeStatesPg) -> None:
    result = await svc.enqueue_batch([])
    assert result.outbox_id is None
    assert not pg.outbox


def test_record_keys_are_injective_for_any_key_text() -> None:
    from tai42_skeleton.states.outbox.keys import record_key, subject_key

    a = record_key("s", StateSubject(target_kind="agent", target_name="a|b", kind="thread", key="c"))
    b = record_key("s", StateSubject(target_kind="agent", target_name="a", kind="thread", key="b|c"))
    assert a != b
    assert json.loads(subject_key("agent", "a", "thread", 'k"[]')) == ["agent", "a", "thread", 'k"[]']
