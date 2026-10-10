"""The states facet's unit of work — staging, projection-served reads, the commit as one pending save.

Driven against the faithful in-memory Postgres (:class:`FakeStatesPg`) so the REAL
``StatesService`` + ``PostgresStatesStore`` run: the projection is computed with the same
applier the persisted write path runs, and ``commit`` writes one ``state_outbox`` row that the
applier then lands through the true store transaction (whole-subject rollback, the
op-idempotency ledger, the guard filter). Off the serving loop — as here — the commit applies the
save's records inline before it returns. Covers: a refused apply lands nothing and holds its
subjects; a staged batch commits once; a re-staged ``op_id`` in a later unit answers
``applied=False``; discard vs commit; a read after a staged update sees the staged value (the
``read`` door and a template program); a nested savepoint failure rolls back only its own
writes; a staged/applied divergence is reported by the applier; an unresolved unit is discarded at
teardown.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states import StateAttach, StateBinding, StateUpdate
from tai42_contract.states.errors import (
    RegimeViolationError,
    StatePendingSaveFailedError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    AttachBody,
    StateBatchWrite,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    StateUnitClosedError,
    WriteOrigin,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.service.unit import current_state_unit
from tai42_skeleton.states.store import PostgresStatesStore
from tai42_skeleton.tools.state_binding import apply_binding_updates

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


async def test_a_root_task_does_not_inherit_the_ambient_unit(svc: StatesService) -> None:
    # A unit of work is bound to the caller's scope alone: a root of execution spawned inside
    # it (``spawn_root_task``) inherits no ambient unit, so its reads go to the store rather
    # than the caller's staging. An ordinary ``create_task`` WOULD copy the unit; the contrast
    # is the point.
    seen: dict = {}

    async def _in_root() -> None:
        seen["root"] = current_state_unit()

    async def _in_copied() -> None:
        seen["copied"] = current_state_unit()

    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        assert current_state_unit() is unit
        root = spawn_root_task(_in_root())
        copied = asyncio.create_task(_in_copied())
        await asyncio.gather(root, copied)

    assert seen["root"] is None  # the root task saw no ambient unit
    assert seen["copied"] is unit  # a plain copy saw the caller's unit


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


async def test_commit_lands_all_staged_writes_in_one_transaction(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        await unit.stage([_write(_subject(), [_set("note", "hi")])])
        result = await unit.commit()
    assert result.outbox_id == "1"
    assert result.deferred_calls == 0
    assert [r.applied for r in result.results] == [True, True]
    assert not pg.outbox  # applied and removed in the apply's transaction
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 1, "note": "hi"}


async def test_the_write_doors_stage_into_an_open_unit_and_a_discard_writes_nothing(
    svc: StatesService, pg: FakeStatesPg
) -> None:
    # Every write door (apply, merge-through-apply, replace) run while a unit is the ambient unit
    # STAGES into it rather than touching the store — so a discard truly discards every one.
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await svc.apply("notes", _subject("t1"), [_set("n", 1)], op_id=None, origin=_ORIGIN)
        await svc.merge("notes", _subject("t2"), {"note": "hi"}, origin=_ORIGIN)
        await svc.replace("notes", _subject("t3"), {"n": 3}, origin=_ORIGIN)
        assert not pg.records  # staged in the unit, nothing in the store
        await unit.discard()
    assert not pg.records  # the discard drops every door's write — none leaked to the store


async def test_merge_through_a_door_reads_back_its_patch_and_a_later_read_sees_it(
    svc: StatesService, pg: FakeStatesPg
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        # Prime the subject's projection so the read-back is served from the unit — the bug's precondition.
        await svc.apply("notes", _subject(), [_set("n", 1)], op_id=None, origin=_ORIGIN)
        merged = await svc.merge("notes", _subject(), {"note": "hello"}, origin=_ORIGIN)
        assert merged.data == {"n": 1, "note": "hello"}  # the merge returns a record carrying its own patch
        later = await svc.read("notes", _subject())
        assert later is not None
        assert later.data == {"n": 1, "note": "hello"}  # a later read in the same drive sees it
        assert not pg.records
        await unit.discard()


async def test_replace_through_a_door_reads_back_within_the_drive(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await svc.apply("notes", _subject(), [_set("n", 1)], op_id=None, origin=_ORIGIN)
        replaced = await svc.replace("notes", _subject(), {"note": "x"}, origin=_ORIGIN)
        assert replaced.data == {"note": "x"}  # a whole-document replace — the earlier n is gone
        later = await svc.read("notes", _subject())
        assert later is not None
        assert later.data == {"note": "x"}
        assert not pg.records
        await unit.discard()


async def test_commit_applies_the_final_state_without_stale_replay_over_a_later_replace(svc: StatesService) -> None:
    # A later whole-document replace must win at commit: the earlier staged op does NOT replay on top of it
    # (the bug let the earlier op resurrect a field the later replace had wiped, with no divergence signal).
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await svc.apply("notes", _subject(), [_set("n", 1)], op_id=None, origin=_ORIGIN)
        await svc.replace("notes", _subject(), {"note": "final"}, origin=_ORIGIN)
        await unit.commit()
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"note": "final"}  # n=1 did not replay over the later replace


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


async def test_a_save_refused_at_apply_lands_nothing_and_holds_its_subjects(
    svc: StatesService, pg: FakeStatesPg, caplog: pytest.LogCaptureFixture
) -> None:
    # The schema narrows between stage and the apply: the save fails as a whole, nothing lands,
    # and every subject it writes is held until an operator retries or discards it.
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject("t1"), [_set("n", 1)])])
        await unit.stage([_write(_subject("t2"), [_set("n", 2)])])
        # No record yet, so the narrowing re-declare is accepted.
        await svc.put_declaration(_decl({"type": "object", "properties": {"n": {"type": "string"}}}))
        with caplog.at_level(logging.ERROR, logger="tai42_skeleton.states.outbox.loud"):
            result = await unit.commit()
    assert result.outbox_id == "1"
    assert not pg.records
    row = pg.outbox[1]
    assert row["status"] == "failed"
    assert row["failed_phase"] == "records"
    assert "ValueValidationError" in row["last_error"]
    assert "pending save 1 failed in its records phase" in caplog.text
    for key in ("t1", "t2"):
        with pytest.raises(StatePendingSaveFailedError, match="has a failed pending save 1") as raised:
            await svc.read("notes", _subject(key))
        assert raised.value.extra == {"save_id": "1"}


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
    assert result.results[0].skipped == staged[0].skipped


async def test_an_apply_whose_ledger_moved_reports_the_divergence(
    svc: StatesService, caplog: pytest.LogCaptureFixture
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        staged = await unit.stage([_write(_subject(), [_set("n", 1)], op_id="k1")])
        assert staged[0].applied is True  # k1 not yet in the ledger at stage time
        # Another writer commits op_id k1 before this unit's save applies — a root of execution so
        # its write goes to the store rather than staging into this scope's unit.
        await spawn_root_task(svc.apply("notes", _subject(), [_set("n", 9)], op_id="k1", origin=_ORIGIN))
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.states.outbox.apply"):
            result = await unit.commit()
    assert result.results[0].applied is True  # the provisional answer the run was served
    assert "pending save 1 diverged from its projection" in caplog.text
    assert "'field': 'applied', 'staged': True, 'applied': False" in caplog.text
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 9}  # the replay found k1 in the ledger and wrote nothing


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


@pytest.mark.parametrize("close", ["commit", "discard"])
async def test_a_closed_unit_raises_the_typed_closed_error(svc: StatesService, close: str) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        await getattr(unit, close)()
        message = "this unit of work is already committed or discarded"
        with pytest.raises(StateUnitClosedError, match=message):
            await unit.stage([_write(_subject(), [_set("n", 2)])])
        with pytest.raises(StateUnitClosedError, match=message):
            await unit.stage_replace("notes", _subject(), {"n": 2}, _ORIGIN)
        with pytest.raises(StateUnitClosedError, match=message):
            async with unit.savepoint():
                pass
        with pytest.raises(StateUnitClosedError, match=message):
            await unit.commit()
        with pytest.raises(StateUnitClosedError, match=message):
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


async def test_an_apply_whose_guard_now_skips_reports_the_divergence(
    svc: StatesService, caplog: pytest.LogCaptureFixture
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        # The guard expects n absent; the empty base satisfies it, so the op applies at stage.
        staged = await unit.stage([_write(_subject(), [_set("n", 1, guard={"path": ["n"], "expected": None})])])
        assert staged[0].applied is True
        assert staged[0].skipped == []
        # Another writer sets n before this unit's save applies (a root of execution, so the write
        # lands in the store, not this scope's unit); now the guard fails and the op skips.
        await spawn_root_task(svc.apply("notes", _subject(), [_set("n", 5)], op_id=None, origin=_ORIGIN))
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.states.outbox.apply"):
            result = await unit.commit()
    assert result.results[0].skipped == []
    assert "'field': 'skipped'" in caplog.text
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 5}


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


class _RenderRM:
    """The resource-manager subset the binding render seam calls: an inline slot renders to its own text."""

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        assert text.content is not None
        return text.content


def _binding_app(svc: StatesService) -> Any:
    return SimpleNamespace(states=svc, storage=SimpleNamespace(resource_manager=_RenderRM()))


# A subject_expr jq yielding ``_subject()`` as a full object, so no ambient context is needed.
_SUBJECT_JQ = '{target_kind: "agent", target_name: "a", kind: "thread", key: "t1"}'


def _n7_binding() -> StateBinding:
    return StateBinding(
        states=[
            StateAttach(
                state="notes",
                subject_expr=TemplatedText(content=_SUBJECT_JQ),
                updates=[StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["n"], value: 7}]'))],
            )
        ]
    )


async def test_binding_updates_write_the_store_directly_with_no_unit_open(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    await apply_binding_updates(_binding_app(svc), _n7_binding(), {}, {}, door_id="d1")
    assert pg.records  # no unit open — the write lands in the store directly
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 7}


async def test_binding_updates_stage_into_an_open_unit_read_back_and_a_discard_clears_them(
    svc: StatesService, pg: FakeStatesPg
) -> None:
    # The door/preset binding-update seam honours an open unit exactly as the engine's node-binding seam
    # does: stage into it (read-your-writes within the drive), never write around it; a discard clears it.
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await apply_binding_updates(_binding_app(svc), _n7_binding(), {}, {}, door_id="d1")
        assert not pg.records  # staged into the unit, not written around it
        view = await svc.read("notes", _subject())
        assert view is not None
        assert view.data == {"n": 7}  # read-your-writes within the drive
        await unit.discard()
    assert not pg.records  # the discard cleared the staged binding write


async def test_binding_updates_commit_lands_through_the_unit(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await apply_binding_updates(_binding_app(svc), _n7_binding(), {}, {}, door_id="d1")
        result = await unit.commit()
    assert result.outbox_id is not None
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 7}


# -- once-per-batch validation and the compare-and-set commit -------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self.stage = 0
        self.commit = 0
        self.committing = False


@pytest.fixture
def validations(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    """Count every whole-document validation, split stage / commit by the commit window."""
    from tai42_skeleton.states.service import unit as unit_mod
    from tai42_skeleton.states.store import writes as writes_mod

    counter = _Counter()
    real = unit_mod._validate_document

    def counted(*args: Any, **kwargs: Any) -> None:
        if counter.committing:
            counter.commit += 1
        else:
            counter.stage += 1
        real(*args, **kwargs)

    monkeypatch.setattr(unit_mod, "_validate_document", counted)
    monkeypatch.setattr(writes_mod, "_validate_document", counted)
    return counter


@pytest.fixture
def cas(svc: StatesService, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record each compare-and-set subject write's outcome (``True`` landed, ``False`` replayed)."""
    outcomes: list[bool] = []
    real = svc._store.write_projected

    async def spy(*args: Any, **kwargs: Any) -> Any:
        landed, seq = await real(*args, **kwargs)
        outcomes.append(landed)
        return landed, seq

    monkeypatch.setattr(svc._store, "write_projected", spy)
    return outcomes


async def _commit(unit: Any, counter: _Counter | None = None) -> Any:
    if counter is not None:
        counter.committing = True
    try:
        return await unit.commit()
    finally:
        if counter is not None:
            counter.committing = False


async def test_a_refused_batch_leaves_nothing_of_itself_staged(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("note", "kept")])])
        with pytest.raises(ValueValidationError):
            await unit.stage(
                [
                    _write(_subject(), [_set("n", 1)], op_id="first"),
                    _write(_subject("t2"), [_set("n", "not-an-integer")]),
                ]
            )
        view = await svc.read("notes", _subject())
        assert view is not None
        assert view.data == {"note": "kept"}  # the refused batch's first item is not staged
        assert await svc.read("notes", _subject("t2")) is None
        result = await unit.commit()
    assert len(result.results) == 1
    # The refused batch's op_id was released with it: a later stage of it applies.
    async with svc.open_unit() as later:
        staged = await later.stage([_write(_subject(), [_set("n", 1)], op_id="first")])
        assert staged[0].applied is True
        await later.discard()


async def test_stage_validates_once_per_touched_subject(svc: StatesService, validations: _Counter) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage(
            [
                _write(_subject("t1"), [_set("n", 1)]),
                _write(_subject("t1"), [_set("note", "a")]),
                _write(_subject("t1"), [_set("n", 2)]),
                _write(_subject("t2"), [_set("n", 3)]),
            ]
        )
        assert validations.stage == 2
        await unit.stage([_write(_subject("t1"), [_set("n", 4)])])
        assert validations.stage == 3
        await unit.discard()


async def test_the_refusal_names_the_state_and_subject_not_the_item(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        with pytest.raises(
            ValueValidationError,
            match=r"state 'notes' subject agent/a/thread/t1: record invalid under the state schema at \$\.n",
        ):
            await unit.stage([_write(_subject(), [_set("n", "x")])])
        await unit.discard()


async def test_a_batch_whose_final_document_is_valid_is_accepted(svc: StatesService) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        staged = await unit.stage([_write(_subject(), [_set("n", "intermediate")]), _write(_subject(), [_set("n", 2)])])
        assert staged[1].data == {"n": 2}
        await unit.commit()
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 2}


async def test_an_unmoved_commit_writes_the_projection_with_no_validation(
    svc: StatesService, pg: FakeStatesPg, validations: _Counter, cas: list[bool]
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject("t1"), [_set("n", 1)]), _write(_subject("t1"), [_set("note", "x")])])
        await unit.stage([_write(_subject("t2"), [_set("n", 2)])])
        result = await _commit(unit, validations)
    assert validations.commit == 0
    assert cas == [True, True]
    assert [r.applied for r in result.results] == [True, True, True]
    view = await svc.read("notes", _subject("t1"))
    assert view is not None
    assert view.data == {"n": 1, "note": "x"}
    # One state_writes row per item, sharing the subject's one committed seq.
    t1_rows = [w for w in pg.writes if w["subject_key"] == "t1"]
    assert [w["paths"] for w in t1_rows] == [[["n"]], [["note"]]]
    assert len({w["seq"] for w in t1_rows}) == 1


async def test_a_moved_base_replays_and_validates_once_per_batch(
    svc: StatesService, pg: FakeStatesPg, validations: _Counter, cas: list[bool]
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        # An intermediate document that is invalid, a final one that is valid.
        await unit.stage([_write(_subject(), [_set("n", "x")]), _write(_subject(), [_set("n", 2)])])
        await unit.stage([_write(_subject(), [_set("note", "b")])])
        await spawn_root_task(svc.apply("notes", _subject(), [_set("note", "moved")], op_id=None, origin=_ORIGIN))
        result = await _commit(unit, validations)
    assert cas == [False]
    assert validations.commit == 2  # once per (batch, subject)
    assert [r.applied for r in result.results] == [True, True, True]
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 2, "note": "b"}


async def test_a_version_bump_between_stage_and_commit_replays(
    svc: StatesService, pg: FakeStatesPg, cas: list[bool]
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        await svc.put_declaration(_decl({"type": "object", "properties": {"n": {"type": "integer"}, "x": {}}}))
        await unit.commit()
    assert cas == [False]
    view = await svc.read("notes", _subject())
    assert view is not None
    assert view.data == {"n": 1}


async def test_an_invalid_final_document_on_replay_fails_the_save_and_lands_nothing(
    svc: StatesService, pg: FakeStatesPg, cas: list[bool]
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject("t1"), [_set("n", 1)])])
        await unit.stage([_write(_subject("t2"), [_set("n", 2)])])
        await svc.put_declaration(_decl({"type": "object", "properties": {"n": {"type": "string"}}}))
        await unit.commit()
    assert not pg.records
    assert not pg.writes
    assert pg.outbox[1]["status"] == "failed"
    assert "state 'notes' subject agent/a/thread/t1" in pg.outbox[1]["last_error"]


async def test_replayed_rows_carry_each_items_stage_time_origin(
    svc: StatesService, pg: FakeStatesPg, cas: list[bool]
) -> None:
    from tai42_contract.states.models import StateContext, SubjectCandidates

    from tai42_skeleton.states.context import state_context

    await svc.put_declaration(_decl())
    ctx = StateContext(
        door="conversation",
        candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "t1"}),
        actor="alice",
        turn_id="turn-1",
        inbound_id="in-1",
    )
    async with svc.open_unit() as unit:
        with state_context(ctx):
            await unit.stage([_write(_subject(), [_set("n", 1)])])
        await spawn_root_task(svc.apply("notes", _subject(), [_set("note", "moved")], op_id=None, origin=_ORIGIN))
        await unit.commit()
    assert cas == [False]
    replayed = [w for w in pg.writes if w["paths"] == [["n"]]]
    assert [(w["door"], w["actor"], w["turn_id"]) for w in replayed] == [("conversation", "alice", "turn-1")]


async def test_projected_rows_carry_each_items_paths_and_origin(svc: StatesService, pg: FakeStatesPg) -> None:
    from tai42_contract.states.models import StateContext, SubjectCandidates

    from tai42_skeleton.states.context import state_context

    await svc.put_declaration(_decl())
    ctx = StateContext(
        door="hook",
        candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "t1"}),
        actor="bob",
    )
    async with svc.open_unit() as unit:
        with state_context(ctx):
            await unit.stage([_write(_subject(), [_set("n", 1)], op_id="w1"), _write(_subject(), [_set("note", "y")])])
        await unit.commit()
    rows = [(w["paths"], w["door"], w["actor"], w["op_id"]) for w in pg.writes]
    assert rows == [([["n"]], "hook", "bob", "w1"), ([["note"]], "hook", "bob", None)]
    assert "w1" in pg.applied_ops


async def test_commit_writes_on_a_callers_connection_opens_no_transaction(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)])])
        staged, records = list(unit._staged), list(unit._records)
        await unit.discard()

    class _RollbackError(Exception):
        pass

    observed: dict[str, Any] = {}

    async def _commit_then_roll_back() -> None:
        async with svc._store.begin() as conn:
            before = pg.transactions
            results = await svc._commit_writes(staged, staged=records, conn=conn)
            observed["own_transactions"] = pg.transactions - before
            observed["applied"] = results[0].applied
            observed["written"] = bool(pg.records)
            raise _RollbackError

    with pytest.raises(_RollbackError):
        await _commit_then_roll_back()
    assert observed == {"own_transactions": 0, "applied": True, "written": True}
    assert not pg.records  # the caller's rollback undid every write


async def test_a_concurrent_ledger_insert_of_a_staged_op_id_falls_back_to_replay(
    svc: StatesService, pg: FakeStatesPg, cas: list[bool]
) -> None:
    await svc.put_declaration(_decl())
    async with svc.open_unit() as unit:
        await unit.stage([_write(_subject(), [_set("n", 1)], op_id="shared")])
        # Another writer lands the same op_id without touching this subject's record.
        await spawn_root_task(svc.apply("notes", _subject("t9"), [_set("n", 9)], op_id="shared", origin=_ORIGIN))
        result = await unit.commit()
    assert cas == [False]
    assert result.results[0].applied is True  # the provisional answer; the applier reports the divergence
    assert await svc.read("notes", _subject()) is None


@pytest.mark.parametrize("staged_kind", ["replace", "ops", "template_jq"])
async def test_a_write_replayed_onto_a_recreated_state_is_admitted_by_the_new_declaration(
    svc: StatesService, pg: FakeStatesPg, cas: list[bool], staged_kind: str
) -> None:
    """Every staged write kind replayed after its state was deleted and re-declared with other
    subject kinds is refused by the re-declared kinds: the save fails loudly and nothing of it lands."""
    await _with_template(svc)
    async with svc.open_unit() as unit:
        if staged_kind == "replace":
            await unit.stage_replace("notes", _subject(), {"m": 1}, _ORIGIN)
        elif staged_kind == "ops":
            await unit.stage([_write(_subject(), [_set("m", 1)])])
        else:
            await unit.stage(
                [
                    StateBatchWrite(
                        state="notes", subject=_subject(), template_jq="add", input={"id": "x"}, origin=_ORIGIN
                    )
                ]
            )
        await svc.delete_declaration("notes")
        await svc.put_declaration(
            StateDeclaration(name="notes", schema=_SCHEMA, subject_kinds=["case"], default_subject_kind="case")
        )
        await unit.commit()
    assert cas == [False]
    assert not pg.records
    assert not pg.writes
    assert pg.outbox[1]["status"] == "failed"
    assert "SubjectRefusedError: subject kind 'thread' is not declared by state 'notes'" in pg.outbox[1]["last_error"]
