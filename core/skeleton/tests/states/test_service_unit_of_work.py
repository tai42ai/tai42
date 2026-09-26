"""The states facet's unit of work — staging, projection-served reads, one-transaction commit.

Driven against the faithful in-memory Postgres (:class:`FakeStatesPg`) so the REAL
``StatesService`` + ``PostgresStatesStore`` run: the projection is computed with the same
applier the persisted write path runs, and ``commit`` replays through the true store
transaction (whole-batch rollback, the op-idempotency ledger, the guard filter). Covers the
design cases: A-then-B-fails commits nothing; a staged batch commits once; a re-staged ``op_id``
in a later unit answers ``applied=False``; discard vs commit; a read after a staged update sees
the staged value (the ``read`` door and a template program); a nested savepoint failure rolls
back only its own writes; a staged/commit divergence is reported; an unresolved unit is
discarded at teardown.
"""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import RegimeViolationError, ValueValidationError
from tai42_contract.states.models import (
    AttachBody,
    StateBatchWrite,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    WriteOrigin,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg
from .fake_service_store import _FakeApp

_ORIGIN = WriteOrigin(consumer="run")

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}, "note": {"type": "string"}}}


def _decl(schema: dict | None = None) -> StateDeclaration:
    return StateDeclaration(
        name="notes",
        schema=schema if schema is not None else _SCHEMA,
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )


def _subject(key: str = "t1") -> StateSubject:
    return StateSubject(target_kind="agent", target_name="a", kind="thread", key=key)


def _set(field: str, value: object, *, guard: dict | None = None) -> dict:
    op: dict = {"op": "set", "path": [field], "value": value}
    if guard is not None:
        op["guard"] = guard
    return op


def _write(subject: StateSubject, ops: list[dict], *, op_id: str | None = None) -> StateBatchWrite:
    return StateBatchWrite(state="notes", subject=subject, ops=ops, op_id=op_id, origin=_ORIGIN)


@pytest.fixture
def svc(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=PostgresStatesStore())


async def test_stage_projects_without_writing_the_store(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        results = await unit.stage([_write(_subject(), [_set("n", 1)])])
        assert results[0].applied is True
        assert results[0].data == {"n": 1}
        # The staged write lands in the projection, never the store, until commit.
        assert not pg.records


async def test_read_your_own_writes_through_the_unit(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 5)])])
        view = await svc.read("notes", _subject())
        assert view is not None
        assert view.data == {"n": 5}
        assert not pg.records  # still not committed
        await unit.discard()


async def test_commit_lands_all_staged_writes_in_one_transaction(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        await unit.stage([_write(_subject(), [_set("note", "hi")])])
        result = await unit.commit()
    assert result.diverged is False
    assert [r.applied for r in result.results] == [True, True]
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 1, "note": "hi"}


async def test_discard_drops_the_staging(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        await unit.discard()
    assert not pg.records


async def test_unresolved_unit_is_discarded_at_teardown(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        # neither commit nor discard — teardown discards it.
    assert not pg.records


async def test_whole_batch_commit_rolls_back_wholly_and_loudly(svc: StatesService, pg: FakeStatesPg) -> None:
    # A lands (projected) then B fails at commit → nothing committed, loud.
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject("t1"), [_set("n", 1)])])
        await unit.stage([_write(_subject("t2"), [_set("n", 2)])])
        # The schema narrows out of band (no records yet, so the redeclare is allowed); the
        # commit re-reads it authoritatively and every staged write now fails validation.
        await svc.put_declaration(_decl({"type": "object", "properties": {"n": {"type": "string"}}}))
        with pytest.raises(ValueValidationError):
            await unit.commit()
    assert not pg.records


async def test_restage_same_op_id_in_a_later_unit_answers_not_applied(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as first:
        await first.stage([_write(_subject(), [_set("n", 1)], op_id="op-1")])
        await first.commit()  # the first unit commits once
    # A second unit re-stages the same op_id after the first committed.
    async with svc.open_unit() as second:
        staged = await second.stage([_write(_subject(), [_set("n", 99)], op_id="op-1")])
        assert staged[0].applied is False  # the ledger already holds op-1 — provisional agrees
        result = await second.commit()
    assert result.diverged is False
    assert result.results[0].applied is False
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 1}  # the replay never wrote n=99


async def test_restage_same_op_id_within_one_unit_answers_not_applied(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        first = await unit.stage([_write(_subject(), [_set("n", 1)], op_id="dup")])
        second = await unit.stage([_write(_subject(), [_set("n", 2)], op_id="dup")])
        assert first[0].applied is True
        assert second[0].applied is False
        result = await unit.commit()
    assert [r.applied for r in result.results] == [True, False]


async def test_guard_skip_is_reported_and_matches_at_commit(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        # The guard expects n == 5 but the base is empty, so the op guard-skips.
        staged = await unit.stage([_write(_subject(), [_set("n", 1, guard={"path": ["n"], "expected": 5})])])
        assert staged[0].skipped == [{"op": "set", "path": ["n"], "reason": "guard"}]
        result = await unit.commit()
    assert result.diverged is False
    assert result.results[0].skipped == staged[0].skipped


async def test_commit_reports_divergence_when_the_ledger_moves(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        staged = await unit.stage([_write(_subject(), [_set("n", 1)], op_id="k1")])
        assert staged[0].applied is True  # k1 not yet in the ledger at stage time
        # Another writer commits op_id k1 before this unit commits.
        await svc.apply("notes", _subject(), [_set("n", 9)], op_id="k1", origin=_ORIGIN)
        result = await unit.commit()
    assert result.diverged is True
    assert result.results[0].applied is False
    assert result.divergences[0].field == "applied"
    assert result.divergences[0].staged is True
    assert result.divergences[0].committed is False


async def test_savepoint_failure_rolls_back_only_its_own_writes(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])

        async def _failing_child() -> None:
            async with unit.savepoint():
                await unit.stage([_write(_subject(), [_set("note", "child")])])
                mid = await svc.read("notes", _subject())
                assert mid is not None
                assert mid.data == {"n": 1, "note": "child"}
                raise RuntimeError("child failed")

        with pytest.raises(RuntimeError, match="child failed"):
            await _failing_child()
        # The child's delta is gone; the parent's write survives.
        after = await svc.read("notes", _subject())
        assert after is not None
        assert after.data == {"n": 1}
        result = await unit.commit()
    assert [r.applied for r in result.results] == [True]
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 1}


async def test_savepoint_success_keeps_its_writes(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        async with unit.savepoint():
            await unit.stage([_write(_subject(), [_set("note", "kept")])])
        await unit.commit()
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 1, "note": "kept"}


async def test_stage_empty_ops_is_not_applied(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        result = await unit.stage([_write(_subject(), [])])
        assert result[0].applied is False


async def test_a_closed_unit_refuses_further_use(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        await unit.commit()
        with pytest.raises(RuntimeError, match="already committed or discarded"):
            await unit.stage([_write(_subject(), [_set("n", 2)])])
        with pytest.raises(RuntimeError, match="already committed or discarded"):
            await unit.commit()
        with pytest.raises(RuntimeError, match="already committed or discarded"):
            await unit.discard()


async def test_teardown_discards_when_the_scope_raises(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())

    async def _raising_scope() -> None:
        async with svc.open_unit() as unit:
            await unit.stage([_write(_subject(), [_set("n", 1)])])
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _raising_scope()
    assert not pg.records


async def test_commit_reports_a_skipped_divergence(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        # The guard expects n absent; the empty base satisfies it, so the op applies at stage.
        staged = await unit.stage([_write(_subject(), [_set("n", 1, guard={"path": ["n"], "expected": None})])])
        assert staged[0].applied is True
        assert staged[0].skipped == []
        # Another writer sets n before this unit commits; now the guard fails and the op skips.
        await svc.apply("notes", _subject(), [_set("n", 5)], op_id=None, origin=_ORIGIN)
        result = await unit.commit()
    assert result.diverged is True
    assert [d.field for d in result.divergences] == ["skipped"]
    assert result.results[0].skipped == [{"op": "set", "path": ["n"], "reason": "guard"}]


_COMPOSING_TEMPLATE = StateTemplateDocument.model_validate(
    {
        "name": "tagmod",
        "schema": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
            },
        },
        "regimes": [{"path": ["tags"], "regime": "composing"}],
    }
)


async def test_stage_refuses_a_composing_shape_violation(svc: StatesService) -> None:
    await svc.put_declaration(_decl({"type": "object", "properties": {"n": {"type": "integer"}}}))
    await svc.put_template(_COMPOSING_TEMPLATE, replace=False)
    await svc.attach("notes", "tagmod", AttachBody(path=[]))
    async with svc.open_unit() as unit:
        # A whole-path set over a composing path is refused at stage, exactly as an apply would be.
        with pytest.raises(RegimeViolationError, match="composing path"):
            await unit.stage([_write(_subject(), [{"op": "set", "path": ["tags"], "value": []}])])
        # The keyed op over the same composing path projects cleanly.
        staged = await unit.stage(
            [_write(_subject(), [{"op": "set_by_key", "path": ["tags"], "key_field": "id", "value": {"id": "a"}}])]
        )
        assert staged[0].applied is True
        assert staged[0].data == {"tags": [{"id": "a"}]}


_TEMPLATE = StateTemplateDocument.model_validate(
    {
        "name": "summary",
        "schema": {
            "type": "object",
            "properties": {
                "ledger": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
            },
        },
        "template_jq": {
            "count": {"purpose": "input", "jq": {"content": "(.ledger // []) | length"}},
            "add": {
                "purpose": "update",
                "writes": [["ledger"]],
                "jq": {"content": '[{op: "set", path: ["ledger"], value: ((.ledger // []) + [$input])}]'},
            },
        },
    }
)


async def _with_template(svc: StatesService) -> None:
    await svc.put_declaration(_decl({"type": "object", "properties": {"m": {"type": "integer"}}}))
    await svc.put_template(_TEMPLATE, replace=False)
    await svc.attach("notes", "summary", AttachBody(path=[]))


async def test_template_read_after_a_staged_update_sees_the_staged_value(svc: StatesService, pg: FakeStatesPg) -> None:
    await _with_template(svc)
    async with svc.open_unit() as unit:
        await unit.stage(
            [StateBatchWrite(state="notes", subject=_subject(), template_jq="add", input={"id": "x"}, origin=_ORIGIN)]
        )
        # The input program reads the projected record — it sees the staged ledger entry.
        result = await svc.eval_template_jq("notes", _subject(), "count", {})
        assert result.value == 1
        assert not pg.records  # not committed yet
        committed = await unit.commit()
    assert committed.results[0].applied is True
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"ledger": [{"id": "x"}]}
