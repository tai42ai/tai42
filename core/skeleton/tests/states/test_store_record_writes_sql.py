"""The record write SQL — replace, ``apply_ops`` (op-id ledger, guards, ``_trace`` stamping,
composing-shape refusal, prune), RTBF erase, and subject fold — driven against the in-memory
fake Postgres (the ``pg``/``store`` fixtures in ``conftest``).
"""

from __future__ import annotations

import pytest
from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.errors import (
    RegimeViolationError,
    StateNotFoundError,
    SubjectFoldError,
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
        retention_days=30,
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
        "alerts", _subj(), op, op_id="op-1", origin=_ORIGIN, validate_doc=_ok, retention_days=30
    )
    assert first[0] is True
    assert "op-1" in pg.applied_ops
    replay = await store.apply_ops(
        "alerts", _subj(), op, op_id="op-1", origin=_ORIGIN, validate_doc=_ok, retention_days=30
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
        retention_days=30,
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
        retention_days=30,
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
        retention_days=30,
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
        retention_days=30,
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
            retention_days=30,
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
            retention_days=30,
        )


async def test_apply_ops_prunes_expired_ledger_rows(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
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
        retention_days=30,
    )
    assert "stale" not in pg.applied_ops  # opportunistic prune dropped the old row
    assert "fresh" in pg.applied_ops


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
