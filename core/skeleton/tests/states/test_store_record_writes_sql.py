"""The record write SQL — replace, ``apply_ops`` (op-id ledger, guards, ``_trace`` stamping,
composing-shape refusal, the version-gated attachments cache, the UPDATE+write CTE), RTBF
erase, and subject fold — driven against the in-memory fake Postgres (the ``pg``/``store``
fixtures in ``conftest``).
"""

from __future__ import annotations

import pytest
from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.errors import (
    RegimeViolationError,
    StateNotFoundError,
    SubjectFoldError,
    ValueValidationError,
)
from tai42_contract.states.models import CompletedOrigin, StateSubject

from tai42_skeleton.states.schema import _validate_document
from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg

_ORIGIN = CompletedOrigin(
    consumer="state_apply",
    meta={"node": "n1"},
    run_id="r1",
    door="conversation",
    actor="alice",
    turn_id="t1",
    inbound_id="i1",
)


def _ok(schema, doc):
    """A permissive document validator — the SQL tests isolate the store, not the schema."""
    return None


async def _admit(subject_kinds):
    """A permissive in-txn subject check — these SQL tests isolate the store, not admission."""
    return None


def _subj(key="t1", kind="thread", tk: ConversationTargetKind = "agent", tn="a"):
    return StateSubject(target_kind=tk, target_name=tn, kind=kind, key=key)


async def test_replace_writes_record_and_provenance(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    data, seq = await store.replace("alerts", _subj(), {"n": 1}, origin=_ORIGIN, validate_doc=_ok)
    assert data == {"n": 1}
    assert seq > 0
    assert len(pg.writes) == 1
    w = pg.writes[0]
    assert w["door"] == "conversation"
    assert w["actor"] == "alice"
    assert w["paths"] == [[]]
    assert w["op_id"] is None


async def test_replace_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.replace("nope", _subj(), {"n": 1}, origin=_ORIGIN, validate_doc=_ok)


async def test_apply_ops_applies_and_records_touched_paths(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    applied, data, seq, skipped = await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set", "path": ["n"], "value": 5}],
        op_id=None,
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )
    assert applied is True
    assert data == {"n": 5}
    assert seq is not None
    assert skipped == []
    assert pg.writes[-1]["paths"] == [["n"]]


async def test_apply_ops_op_id_ledger_and_replay(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    op = [{"op": "set", "path": ["n"], "value": 1}]
    first = await store.apply_ops(
        "alerts", _subj(), op, op_id="op-1", origin=_ORIGIN, validate_doc=_ok, validate_subject_in_txn=_admit
    )
    assert first[0] is True
    assert "op-1" in pg.applied_ops
    replay = await store.apply_ops(
        "alerts", _subj(), op, op_id="op-1", origin=_ORIGIN, validate_doc=_ok, validate_subject_in_txn=_admit
    )
    assert replay == (False, None, None, [])
    # only the first apply wrote a record-changing row
    assert len([w for w in pg.writes if w["op_id"] == "op-1"]) == 1


async def test_apply_ops_guard_skips_and_deletes_fresh_record(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    applied, data, seq, skipped = await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set", "path": ["n"], "value": 5, "guard": {"path": ["n"], "expected": 99}}],
        op_id=None,
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )
    assert applied is True
    assert data is None  # the freshly-inserted empty record was rolled back out
    assert seq is None
    assert len(skipped) == 1
    assert await store.read_record("alerts", _subj()) == (None, None)


async def test_apply_ops_guard_pass_on_existing_record(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    applied, data, _seq, skipped = await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set", "path": ["n"], "value": 2, "guard": {"path": ["n"], "expected": 1}}],
        op_id=None,
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )
    assert applied is True
    assert data == {"n": 2}
    assert skipped == []


async def test_apply_ops_all_guards_skip_on_existing_keeps_record(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    applied, data, seq, skipped = await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set", "path": ["n"], "value": 2, "guard": {"path": ["n"], "expected": 99}}],
        op_id=None,
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )
    assert applied is True
    assert data == {"n": 1}  # the existing record is untouched, not deleted
    assert seq is not None
    assert len(skipped) == 1


async def test_apply_ops_stamps_trace_under_traced_attach(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.templates["m"] = {
        "name": "m",
        "body": {"kind": "state-template", "name": "m", "schema": {"type": "object"}, "trace": {"enabled": True}},
        "shipped_hash": None,
        "updated_at": pg.tick(),
    }
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    applied, data, _seq, _sk = await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set_by_key", "path": ["a", "items"], "key_field": "id", "value": {"id": 1}}],
        op_id=None,
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )
    assert applied is True
    assert data is not None
    trace = data["a"]["items"][0]["_trace"]
    assert set(trace) == {"meta", "run", "turn", "inbound", "at"}
    assert trace["meta"] == {"node": "n1"}
    assert trace["run"] == "r1"
    assert isinstance(trace["at"], str)


async def test_apply_ops_refuses_composing_shape_before_ledger(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.templates["m"] = {
        "name": "m",
        "body": {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object"},
            "regimes": [{"path": ["items"], "regime": "composing"}],
        },
        "shipped_hash": None,
        "updated_at": pg.tick(),
    }
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    with pytest.raises(RegimeViolationError):
        await store.apply_ops(
            "alerts",
            _subj(),
            [{"op": "set", "path": ["a", "items"], "value": []}],
            op_id="op-x",
            origin=_ORIGIN,
            validate_doc=_ok,
            validate_subject_in_txn=_admit,
        )
    assert "op-x" not in pg.applied_ops  # the shape refusal precedes the ledger insert


async def test_apply_ops_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.apply_ops(
            "nope",
            _subj(),
            [{"op": "set", "path": ["n"], "value": 1}],
            op_id=None,
            origin=_ORIGIN,
            validate_doc=_ok,
            validate_subject_in_txn=_admit,
        )


async def test_apply_ops_does_not_prune_the_op_ledger(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    from datetime import timedelta

    pg.seed_declaration("alerts")
    pg.applied_ops["stale"] = pg.now() - timedelta(days=40)
    await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set", "path": ["n"], "value": 1}],
        op_id="fresh",
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )
    # The write path never prunes: the op ledger's retention is the state-retention sweep's job.
    assert "stale" in pg.applied_ops
    assert "fresh" in pg.applied_ops
    assert not any("state_applied_ops" in sql and "DELETE" in sql for sql, _ in pg.executed)


async def test_apply_ops_round_trip_count_cold_then_warm(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    ops = [{"op": "set", "path": ["n"], "value": 1}]

    before = len(pg.executed)
    await store.apply_ops(
        "alerts", _subj(), ops, op_id="a", origin=_ORIGIN, validate_doc=_ok, validate_subject_in_txn=_admit
    )
    cold = len(pg.executed) - before
    # cold path: FOR SHARE decl, attachments JOIN, alias resolve, op-ledger insert,
    # record upsert-lock, and the UPDATE+write CTE.
    assert cold == 6

    before = len(pg.executed)
    await store.apply_ops(
        "alerts", _subj(), ops, op_id="b", origin=_ORIGIN, validate_doc=_ok, validate_subject_in_txn=_admit
    )
    warm = len(pg.executed) - before
    # warm path skips the attachments JOIN (the declaration's version is unchanged).
    assert warm == 5


async def test_apply_ops_attachment_cache_serves_the_warm_composition(
    pg: FakeStatesPg, store: PostgresStatesStore
) -> None:
    pg.seed_declaration("alerts")
    ops = [{"op": "set", "path": ["n"], "value": 1}]

    def _joins() -> int:
        return sum(1 for sql, _ in pg.executed if "JOIN state_templates" in sql)

    await store.apply_ops(
        "alerts", _subj(), ops, op_id="a", origin=_ORIGIN, validate_doc=_ok, validate_subject_in_txn=_admit
    )
    assert _joins() == 1  # cold miss reads the composition
    await store.apply_ops(
        "alerts", _subj(), ops, op_id="b", origin=_ORIGIN, validate_doc=_ok, validate_subject_in_txn=_admit
    )
    assert _joins() == 1  # warm hit — no second JOIN


def _joins(pg: FakeStatesPg) -> int:
    return sum(1 for sql, _ in pg.executed if "JOIN state_templates" in sql)


async def _apply_once(store: PostgresStatesStore, op_id: str) -> None:
    await store.apply_ops(
        "alerts",
        _subj(),
        [{"op": "set", "path": ["n"], "value": 1}],
        op_id=op_id,
        origin=_ORIGIN,
        validate_doc=_ok,
        validate_subject_in_txn=_admit,
    )


def _seed_template(pg: FakeStatesPg, name: str = "m") -> None:
    pg.templates[name] = {
        "name": name,
        "body": {"kind": "state-template", "name": name, "schema": {"type": "object"}},
        "shipped_hash": None,
        "updated_at": pg.tick(),
    }


_EFF = {"type": "object"}


async def test_cache_invalidated_by_attach(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    _seed_template(pg)
    await _apply_once(store, "a")
    assert _joins(pg) == 1
    await store.upsert_attachment("alerts", "m", ["a"], {}, {}, effective_schema=_EFF)
    await _apply_once(store, "b")
    assert _joins(pg) == 2  # attach bumped updated_at → cache miss


async def test_cache_invalidated_by_update_attachment_declarations(
    pg: FakeStatesPg, store: PostgresStatesStore
) -> None:
    pg.seed_declaration("alerts")
    _seed_template(pg)
    await store.upsert_attachment("alerts", "m", ["a"], {}, {}, effective_schema=_EFF)
    await _apply_once(store, "a")
    joins = _joins(pg)
    await store.update_attachment_declarations("alerts", "m", {"x": 1}, effective_schema=_EFF)
    await _apply_once(store, "b")
    assert _joins(pg) == joins + 1


async def test_cache_invalidated_by_template_body_replace(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    # A template-body replace backfills each attached state through update_attachment_parameters,
    # which bumps the declaration's updated_at.
    pg.seed_declaration("alerts")
    _seed_template(pg)
    await store.upsert_attachment("alerts", "m", ["a"], {}, {}, effective_schema=_EFF)
    await _apply_once(store, "a")
    joins = _joins(pg)
    await store.update_attachment_parameters("alerts", "m", {"p": 1}, effective_schema=_EFF)
    await _apply_once(store, "b")
    assert _joins(pg) == joins + 1


async def test_cache_invalidated_by_detach(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    _seed_template(pg)
    await store.upsert_attachment("alerts", "m", ["a"], {}, {}, effective_schema=_EFF)
    await _apply_once(store, "a")
    joins = _joins(pg)
    await store.delete_attachment("alerts", "m", effective_schema=_EFF)
    await _apply_once(store, "b")
    assert _joins(pg) == joins + 1


async def test_cache_invalidated_by_declaration_upsert(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    await _apply_once(store, "a")
    joins = _joins(pg)
    await store.upsert_declaration("alerts", "", {"type": "object"}, ["thread"], "thread", None)
    await _apply_once(store, "b")
    assert _joins(pg) == joins + 1


async def test_apply_ops_schema_failure_rolls_back_write_and_ledger(
    pg: FakeStatesPg, store: PostgresStatesStore
) -> None:
    strict = {"type": "object", "properties": {"n": {"type": "integer"}}, "additionalProperties": False}
    pg.seed_declaration("alerts", schema=strict, effective_schema=strict)
    with pytest.raises(ValueValidationError):
        await store.apply_ops(
            "alerts",
            _subj(),
            [{"op": "set", "path": ["bad"], "value": 1}],
            op_id="op-z",
            origin=_ORIGIN,
            validate_doc=_validate_document,
            validate_subject_in_txn=_admit,
        )
    # The whole transaction rolled back: no op-ledger row, no write row, no record.
    assert "op-z" not in pg.applied_ops
    assert pg.writes == []
    assert await store.read_record("alerts", _subj()) == (None, None)


async def test_erase_declared_is_alias_aware_and_audited(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "new", {"n": 1})
    pg.aliases[("alerts", "agent", "a", "thread", "old")] = {
        "state": "alerts",
        "target_kind": "agent",
        "target_name": "a",
        "alias_kind": "thread",
        "alias_key": "old",
        "canonical_kind": "thread",
        "canonical_key": "new",
        "mode": "switch",
    }
    await store.erase_subject("alerts", _subj(key="old"), origin=_ORIGIN)
    assert await store.read_record("alerts", _subj(key="new")) == (None, None)
    assert pg.aliases == {}  # the alias pointing at the survivor died with it
    assert len(pg.writes) == 1
    assert pg.writes[0]["paths"] == [[]]


async def test_erase_idempotent_no_write_when_absent(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    await store.erase_subject("alerts", _subj(), origin=_ORIGIN)
    assert pg.writes == []  # nothing deleted → no ledger row


async def test_erase_undeclared_plain_delete(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_record("orphan", "agent", "a", "thread", "t1", {"n": 1})
    await store.erase_subject("orphan", _subj(), origin=_ORIGIN)
    assert pg.records == {}
    assert pg.writes == []


async def test_fold_switch_lands_old_key_on_survivor(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    s1, s2 = _subj(key="old"), _subj(key="new")
    await store.replace("alerts", s2, {"n": 2}, origin=_ORIGIN, validate_doc=_ok)
    await store.replace("alerts", s1, {"n": 1}, origin=_ORIGIN, validate_doc=_ok)
    report = await store.fold_subject("alerts", s1, s2, "switch", origin=_ORIGIN, validate_doc=_ok)
    assert report["already"] is False
    assert (await store.read_record("alerts", s1))[0] == {"n": 2}  # old key resolves to survivor
    assert ("alerts", "agent", "a", "thread", "old") in pg.aliases


async def test_fold_merge_moves_absent_members(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration(
        "alerts", schema={"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "string"}}}
    )
    s1, s2 = _subj(key="old"), _subj(key="new")
    await store.replace("alerts", s1, {"x": "from-old", "y": "old-loses"}, origin=_ORIGIN, validate_doc=_ok)
    await store.replace("alerts", s2, {"y": "new-wins"}, origin=_ORIGIN, validate_doc=_ok)
    report = await store.fold_subject("alerts", s1, s2, "merge", origin=_ORIGIN, validate_doc=_ok)
    assert report["merged_members"] == ["x"]
    assert (await store.read_record("alerts", s2))[0] == {"x": "from-old", "y": "new-wins"}


async def test_fold_merge_without_source_record(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    s1, s2 = _subj(key="old"), _subj(key="new")
    await store.replace("alerts", s2, {"n": 2}, origin=_ORIGIN, validate_doc=_ok)
    report = await store.fold_subject("alerts", s1, s2, "merge", origin=_ORIGIN, validate_doc=_ok)
    assert report["merged_members"] == []
    assert ("alerts", "agent", "a", "thread", "old") in pg.aliases


async def test_fold_already_folded_is_noop(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    s1, s2 = _subj(key="old"), _subj(key="new")
    await store.replace("alerts", s2, {"n": 2}, origin=_ORIGIN, validate_doc=_ok)
    await store.replace("alerts", s1, {"n": 1}, origin=_ORIGIN, validate_doc=_ok)
    await store.fold_subject("alerts", s1, s2, "switch", origin=_ORIGIN, validate_doc=_ok)
    again = await store.fold_subject("alerts", s1, s2, "switch", origin=_ORIGIN, validate_doc=_ok)
    assert again["already"] is True


async def test_fold_into_conflicting_canonical_forks_identity(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    s1, s2, s3 = _subj(key="old"), _subj(key="new"), _subj(key="other")
    await store.replace("alerts", s2, {"n": 2}, origin=_ORIGIN, validate_doc=_ok)
    await store.replace("alerts", s1, {"n": 1}, origin=_ORIGIN, validate_doc=_ok)
    await store.fold_subject("alerts", s1, s2, "switch", origin=_ORIGIN, validate_doc=_ok)
    with pytest.raises(SubjectFoldError, match="would fork its identity"):
        await store.fold_subject("alerts", s1, s3, "switch", origin=_ORIGIN, validate_doc=_ok)


async def test_fold_into_self_refused(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    with pytest.raises(SubjectFoldError, match="into itself"):
        await store.fold_subject("alerts", _subj(key="x"), _subj(key="x"), "switch", origin=_ORIGIN, validate_doc=_ok)


async def test_fold_across_targets_refused(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    with pytest.raises(SubjectFoldError, match="cannot fold across targets"):
        await store.fold_subject("alerts", _subj(tn="a"), _subj(tn="b"), "switch", origin=_ORIGIN, validate_doc=_ok)


async def test_fold_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.fold_subject("nope", _subj(key="a"), _subj(key="b"), "switch", origin=_ORIGIN, validate_doc=_ok)


async def test_fold_merge_invalid_document_refused(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration(
        "alerts",
        schema={"type": "object", "properties": {"x": {"type": "string"}}},
        effective_schema={"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False},
    )
    s1, s2 = _subj(key="old"), _subj(key="new")
    pg.seed_record("alerts", "agent", "a", "thread", "old", {"bad": "member"})
    pg.seed_record("alerts", "agent", "a", "thread", "new", {"x": "ok"})
    with pytest.raises(SubjectFoldError, match="would leave an invalid document"):
        await store.fold_subject("alerts", s1, s2, "merge", origin=_ORIGIN, validate_doc=_validate_document)
