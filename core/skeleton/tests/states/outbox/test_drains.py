"""The read-side drains on real Postgres: a pending save lands before its subject is read; a held one refuses loudly.

Covers the record reads and writes, a lock held past the timeout, a save held behind a failed one,
every whole-state scan's raise/skip/count rule, the re-declare guard counting held saves, the write
history, and the rename referee's target drain.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from tai42_contract.states.errors import (
    DeclarationInUseError,
    NonAdditiveRedeclareError,
    StatePendingSaveFailedError,
    StatePendingSaveTimeoutError,
    SubjectFoldError,
)
from tai42_contract.states.models import AttachBody, StateDeclaration, StateTemplateDocument, WriteOrigin
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.states.db import STATES_COMPONENT
from tai42_skeleton.states.outbox.keys import record_key

from .conftest import OutboxBed, execute, set_states_env

pytestmark = pytest.mark.integration

_ORIGIN = WriteOrigin(consumer="c")


def _set(field: str, value: Any) -> dict[str, Any]:
    return {"op": "set", "path": [field], "value": value}


async def _held_pair(bed: OutboxBed) -> tuple[int, int]:
    """Save 1 failed on subject A; save 2 pending on A and B (held behind save 1)."""
    failed, held = await bed.enqueue_together(
        [bed.write(bed.subject("A"), [_set("n", 1)])],
        [bed.write(bed.subject("A"), [_set("n", 2)]), bed.write(bed.subject("B"), [_set("n", 3)])],
    )
    await bed.fail(failed)
    return failed, held


async def test_a_read_with_a_pending_save_applies_it_first(bed: OutboxBed) -> None:
    row = await bed.enqueue(bed.write(bed.subject(), [_set("n", 4)]))
    record = await bed.svc.read(bed.state, bed.subject())
    assert record is not None
    assert record.data == {"n": 4}
    assert await bed.status(row) is None


async def test_a_read_on_a_failed_save_raises_naming_it(bed: OutboxBed) -> None:
    row = await bed.enqueue(bed.write(bed.subject(), [_set("n", 4)]))
    await bed.fail(row)
    with pytest.raises(StatePendingSaveFailedError, match=f"has a failed pending save {row}; an operator") as raised:
        await bed.svc.read(bed.state, bed.subject())
    assert raised.value.extra == {"save_id": str(row)}


async def test_a_lock_held_past_the_drain_timeout_raises_the_timeout(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS="0.5", STATES_OUTBOX_DRAIN_POLL_SECONDS="0.05")
    row = await bed.enqueue(bed.write(bed.subject(), [_set("n", 4)]))
    key = record_key(bed.state, bed.subject())
    async with (
        client_ctx(PostgresClient, component_store_settings(STATES_COMPONENT)) as pool,
        pool.connection() as holder,
        holder.transaction(),
    ):
        await holder.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
        with pytest.raises(StatePendingSaveTimeoutError, match=f"still has pending save {row} after 0.5s") as raised:
            await bed.svc.read(bed.state, bed.subject())
    assert raised.value.extra == {"save_id": str(row)}
    assert await bed.status(row) == "pending"


async def test_a_read_on_a_subject_held_behind_a_failed_save_raises_at_once(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS="0.3", STATES_OUTBOX_DRAIN_POLL_SECONDS="0.05")
    failed, held = await _held_pair(bed)
    with pytest.raises(StatePendingSaveFailedError) as raised:
        await bed.svc.read(bed.state, bed.subject("B"))
    assert f"has pending save {held} held behind failed pending save {failed}" in str(raised.value)
    assert raised.value.extra == {"save_id": str(failed)}


async def test_the_write_history_includes_a_pending_save_and_refuses_on_a_held_subject(bed: OutboxBed) -> None:
    await bed.enqueue(bed.write(bed.subject("A"), [_set("n", 1)]))
    page = await bed.svc.writes(bed.state, bed.subject("A"))
    assert [item.paths for item in page.items] == [[["n"]]]
    failed = await bed.enqueue(bed.write(bed.subject("B"), [_set("n", 1)]))
    await bed.fail(failed)
    with pytest.raises(StatePendingSaveFailedError, match=f"failed pending save {failed}"):
        await bed.svc.writes(bed.state, bed.subject("B"))


@pytest.mark.parametrize("door", ["fold", "erase", "delete_declaration", "restore", "backup export"])
async def test_the_identity_and_whole_state_doors_refuse_on_a_held_save(bed: OutboxBed, door: str) -> None:
    failed, _held = await _held_pair(bed)

    async def _through_the_door() -> None:
        if door == "fold":
            await bed.svc.fold(bed.state, bed.subject("B"), bed.subject("C"), "merge", origin=_ORIGIN)
        elif door == "erase":
            await bed.svc.erase(bed.state, bed.subject("A"), origin=_ORIGIN)
        elif door == "delete_declaration":
            await bed.svc.delete_declaration(bed.state)
        else:
            await bed.svc.drain_pending_saves(bed.state, held="raise", scan=door)

    with pytest.raises(StatePendingSaveFailedError, match=f"failed pending save {failed}"):
        await _through_the_door()


async def test_a_fold_refuses_a_save_enqueued_after_its_drain(bed: OutboxBed, monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.states.service import records as records_mod

    real_drain = records_mod.drain_records

    async def _drain_then_enqueue(service: Any, keys: Any, deadline: float) -> None:
        await real_drain(service, keys, deadline)
        await bed.enqueue(bed.write(bed.subject("B"), [_set("n", 9)]))

    monkeypatch.setattr(records_mod, "drain_records", _drain_then_enqueue)
    with pytest.raises(SubjectFoldError, match="has a pending save; retry the fold"):
        await bed.svc.fold(bed.state, bed.subject("B"), bed.subject("C"), "merge", origin=_ORIGIN)


async def test_the_skipping_scans_serve_committed_data_and_name_the_held_saves(
    bed: OutboxBed, caplog: pytest.LogCaptureFixture
) -> None:
    await bed.svc.replace(bed.state, bed.subject("C"), {"n": 1}, origin=_ORIGIN)
    failed, held = await _held_pair(bed)
    clean = await bed.enqueue(bed.write(bed.subject("D"), [_set("n", 5)]))
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.states.outbox.drain"):
        page = await bed.svc.list_subjects(bed.state)
        found = await bed.svc.search(bed.state, {"n": 5})
        stats = await bed.svc.stats(bed.state)
    # The un-held save was applied first (help-along); the held ones are named, not applied.
    assert await bed.status(clean) is None
    assert {s["subject"]["key"] for s in page["subjects"]} == {"C", "D"}
    assert [m["subject"]["key"] for m in found["matches"]] == ["D"]
    assert stats["records"] == 2
    held_ids = {(h["save_id"], h["held_by"]) for h in page["held"]}
    assert held_ids == {(str(failed), str(failed)), (str(held), str(failed))}
    assert found["held"] == page["held"] == stats["held"]
    assert f"list_subjects on state {bed.state!r} skipped 2 subject(s) held by failed pending save(s) ['{failed}']" in (
        caplog.text
    )


async def test_retention_keeps_a_held_subjects_record_and_names_the_held_save(bed: OutboxBed) -> None:
    await bed.svc.replace(bed.state, bed.subject("A"), {"n": 1}, origin=_ORIGIN)
    await bed.svc.replace(bed.state, bed.subject("Z"), {"n": 1}, origin=_ORIGIN)
    failed = await bed.enqueue(bed.write(bed.subject("A"), [_set("n", 2)]))
    await bed.fail(failed)
    await execute("UPDATE state_declarations SET retention_days = 1 WHERE name = %s", (bed.state,))
    await execute("UPDATE state_records SET updated_at = now() - interval '3 days' WHERE state = %s", (bed.state,))
    result = await bed.svc.prune_expired()
    assert result.pruned.get(bed.state) == 1
    assert [(h.save_id, h.held_by) for h in result.held if h.save_id == str(failed)] == [(str(failed), str(failed))]
    kept = await execute("SELECT subject_key FROM state_records WHERE state = %s", (bed.state,))
    assert kept == [("A",)]


_TEMPLATE = {
    "name": "",
    "schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
}


async def test_attach_and_declarations_update_proceed_and_return_the_held_saves(
    bed: OutboxBed, caplog: pytest.LogCaptureFixture
) -> None:
    failed, held = await _held_pair(bed)
    template = "tpl-" + bed.state.replace("_", "-")
    await bed.svc.put_template(StateTemplateDocument.model_validate({**_TEMPLATE, "name": template}), replace=False)
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.states.outbox.drain"):
        attached = await bed.svc.attach(bed.state, template, AttachBody(path=["t"]))
        updated = await bed.svc.update_attachment_declarations(bed.state, template, {})
    assert {(h.save_id, h.held_by) for h in attached} == {(str(failed), str(failed)), (str(held), str(failed))}
    assert attached == updated
    assert "attach on state" in caplog.text
    await execute("DELETE FROM state_attachments WHERE state = %s", (bed.state,))
    await execute("DELETE FROM state_templates WHERE name = %s", (template,))


# -- the re-declare guard ----------------------------------------------------------------------------


def _decl(bed: OutboxBed, note: Any, kinds: list[str] | None = None) -> StateDeclaration:
    return StateDeclaration(
        name=bed.state,
        schema={"type": "object", "properties": {"n": {"type": "integer"}, "note": note}},
        subject_kinds=kinds or ["thread", "case"],
        default_subject_kind="thread",
    )


async def _held_note_save(bed: OutboxBed, *, behind: bool) -> int:
    """A held save whose subject thread/t-1 projects ``{"note": "text"}``; ``behind`` holds it behind a failed one."""
    await bed.svc.put_declaration(_decl(bed, {"type": "string"}))
    if not behind:
        row = await bed.enqueue(bed.write(bed.subject("t-1"), [_set("note", "text")]))
        await bed.fail(row)
        return row
    failed, held = await bed.enqueue_together(
        [bed.write(bed.subject("other"), [_set("n", 1)])],
        [bed.write(bed.subject("other"), [_set("n", 2)]), bed.write(bed.subject("t-1"), [_set("note", "text")])],
    )
    await bed.fail(failed)
    return held


async def test_a_pending_save_lands_before_the_guard_counts_its_records(bed: OutboxBed) -> None:
    await bed.svc.put_declaration(_decl(bed, {"type": "string"}, ["thread", "case"]))
    await bed.enqueue(bed.write(bed.subject("t-1"), [_set("note", "text")]))
    with pytest.raises(DeclarationInUseError, match=r"still has records under subject kind\(s\) \['thread'\]"):
        await bed.svc.put_declaration(
            StateDeclaration(
                name=bed.state,
                schema={"type": "object", "properties": {"n": {"type": "integer"}, "note": {"type": "string"}}},
                subject_kinds=["case"],
                default_subject_kind="case",
            )
        )
    with pytest.raises(NonAdditiveRedeclareError):
        await bed.svc.put_declaration(_decl(bed, {"type": "integer"}))


@pytest.mark.parametrize("behind", [False, True])
async def test_a_held_saves_records_are_counted_by_the_re_declare_guard(bed: OutboxBed, behind: bool) -> None:
    held = await _held_note_save(bed, behind=behind)
    with pytest.raises(DeclarationInUseError, match="whose document the new declaration refuses") as refused:
        await bed.svc.put_declaration(_decl(bed, {"type": "integer"}))
    assert f"save {held}, subject thread/t-1" in str(refused.value)
    extra = refused.value.extra
    assert extra is not None
    assert str(held) in {h["save_id"] for h in extra["held"]}
    stored = await bed.svc.get_declaration(bed.state)
    assert stored is not None
    assert isinstance(stored.schema_, dict)
    assert stored.schema_["properties"]["note"] == {"type": "string"}
    with pytest.raises(DeclarationInUseError, match=r"writing subject kind\(s\) \['thread'\]"):
        await bed.svc.put_declaration(
            StateDeclaration(
                name=bed.state,
                schema={"type": "object", "properties": {"n": {"type": "integer"}, "note": {"type": "string"}}},
                subject_kinds=["case"],
                default_subject_kind="case",
            )
        )
    saved = await bed.svc.put_declaration(_decl(bed, {"type": ["integer", "string"]}))
    assert str(held) in {h.save_id for h in saved.held}
    assert any(s.key == "t-1" for h in saved.held for s in h.subjects)


async def test_a_widening_re_declare_then_a_retry_applies_the_held_save(bed: OutboxBed) -> None:
    held = await _held_note_save(bed, behind=False)
    await bed.svc.put_declaration(_decl(bed, {"type": ["integer", "string"]}))
    outcome = await bed.svc.retry_pending_save(held)
    assert outcome.requeued is True
    assert outcome.row is None
    record = await bed.svc.read(bed.state, bed.subject("t-1"))
    assert record is not None
    assert record.data == {"note": "text"}


async def test_with_committed_records_a_widening_re_declare_is_still_refused(bed: OutboxBed) -> None:
    await _held_note_save(bed, behind=False)
    await bed.svc.replace(bed.state, bed.subject("c-1", target_name="b"), {"n": 1}, origin=_ORIGIN)
    with pytest.raises(NonAdditiveRedeclareError, match="erase them first"):
        await bed.svc.put_declaration(_decl(bed, {"type": ["integer", "string"]}))


async def test_a_save_enqueued_between_the_drain_and_the_guard_refuses_the_re_declare(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.states.service import declarations as declarations_mod

    await bed.svc.put_declaration(_decl(bed, {"type": "string"}))
    real_drain = declarations_mod.drain_state
    rows: list[int] = []

    async def _drain_then_enqueue(*args: Any, **kwargs: Any) -> Any:
        out = await real_drain(*args, **kwargs)
        rows.append(await bed.enqueue(bed.write(bed.subject("late"), [_set("n", 1)])))
        return out

    monkeypatch.setattr(declarations_mod, "drain_state", _drain_then_enqueue)
    with pytest.raises(DeclarationInUseError, match=r"written while it was re-declared; retry the re-declare"):
        await bed.svc.put_declaration(_decl(bed, {"type": ["integer", "string"]}))
    assert str(rows[0]) in str(await _message_of(bed))


async def _message_of(bed: OutboxBed) -> str:
    rows = await execute("SELECT id FROM state_outbox WHERE states @> ARRAY[%s]::text[]", (bed.state,))
    return str([r[0] for r in rows])


# -- the rename referee's target drain --------------------------------------------------------------


async def test_the_target_drain_applies_a_pending_save_under_the_target(bed: OutboxBed) -> None:
    subject = bed.subject("t-1", target_kind="tool", target_name=f"T{bed.state}")
    row = await bed.enqueue(bed.write(subject, [_set("n", 1)]))
    lines = await bed.svc.drain_target_saves("tool", f"T{bed.state}")
    assert lines == []
    assert await bed.status(row) is None
    assert await execute("SELECT count(*) FROM state_records WHERE target_name = %s", (f"T{bed.state}",)) == [(1,)]


async def test_the_target_drain_names_a_failed_save_under_the_target(bed: OutboxBed) -> None:
    name = f"T{bed.state}"
    row = await bed.enqueue(bed.write(bed.subject("t-1", target_kind="tool", target_name=name), [_set("n", 1)]))
    await bed.fail(row)
    lines = await bed.svc.drain_target_saves("tool", name)
    assert lines == [
        f"failed pending state save {row} holds writes under target tool/{name}; an operator must retry or discard it"
    ]


async def test_the_target_drain_names_the_failed_save_a_pending_one_is_held_behind(bed: OutboxBed) -> None:
    name = f"T{bed.state}"
    subject = bed.subject("t-1", target_kind="tool", target_name=name)
    older, newer = await bed.enqueue_together(
        [bed.write(subject, [_set("n", 1)])], [bed.write(subject, [_set("n", 2)])]
    )
    await bed.fail(older)
    lines = await bed.svc.drain_target_saves("tool", name)
    line = (
        f"failed pending state save {older} holds writes under target tool/{name}; an operator must retry or discard it"
    )
    assert lines == [line]
    assert await bed.status(newer) == "pending"


async def test_the_target_drain_names_a_pending_save_its_apply_fails(bed: OutboxBed) -> None:
    name = f"T{bed.state}"
    async with bed.svc.open_unit() as unit:
        await unit.stage([bed.write(bed.subject("t-1", target_kind="tool", target_name=name), [_set("n", 1)])])
        # The state narrows while the write is still staged, so the save's apply refuses it.
        await bed.svc.put_declaration(
            StateDeclaration(
                name=bed.state,
                schema={"type": "object", "properties": {"n": {"type": "string"}}},
                subject_kinds=["thread"],
                default_subject_kind="thread",
            )
        )
        row = int((await unit.commit()).outbox_id or 0)
    lines = await bed.svc.drain_target_saves("tool", name)
    assert lines == [
        f"failed pending state save {row} holds writes under target tool/{name}; an operator must retry or discard it"
    ]
    assert await bed.status(row) == "failed"


async def test_the_target_drain_waits_for_running_calls_then_names_them_past_the_deadline(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    from tai42_contract.states.models import StateContext, SubjectCandidates

    from tai42_skeleton.states.context import state_context

    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS="0.4", STATES_OUTBOX_DRAIN_POLL_SECONDS="0.05")
    name = f"T{bed.state}"
    ctx = StateContext(
        door="api", candidates=SubjectCandidates(target_kind="tool", target_name=name, by_kind={"thread": "t-1"})
    )
    with state_context(ctx):
        row = await bed.enqueue(calls=(("echo", {}),))
    await execute("UPDATE state_outbox SET status = 'running', claimed_by = 'other' WHERE id = %s", (row,))
    lines = await bed.svc.drain_target_saves("tool", name)
    assert lines == [f"pending state save {row} under target tool/{name} is still running its deferred calls"]

    async def _finish_soon() -> None:
        await asyncio.sleep(0.1)
        await execute("DELETE FROM state_outbox WHERE id = %s", (row,))

    finisher = asyncio.create_task(_finish_soon())
    assert await bed.svc.drain_target_saves("tool", name) == []
    await finisher
