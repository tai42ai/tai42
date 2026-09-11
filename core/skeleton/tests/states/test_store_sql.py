"""The subject-keyed store's SQL, driven against an in-memory fake Postgres.

Every case runs the REAL :class:`~tai42_skeleton.states.store.PostgresStatesStore` against
``FakeStatesPg`` (the ``pg``/``store`` fixtures in ``conftest``), so each public method's
statement shape — the table it targets, its parameter order, keyset cursor handling, the
``_trace`` stamp, the ``state_writes`` provenance row, the idempotency-ledger insert and
prune, the alias-aware reads, and the loud errors on an undeclared state — runs through its
true SQL with no live database. The real-Postgres semantics (jsonb operators, row locks,
``clock_timestamp``) are additionally exercised in ``test_store_integration.py``.
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

from tai42_skeleton.states.service import _validate_document
from tai42_skeleton.states.store import PostgresStatesStore, make_cursor

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


# --------------------------------------------------------------------------- #
# declarations                                                                  #
# --------------------------------------------------------------------------- #
async def test_get_declaration_hit_and_miss(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    assert await store.get_declaration("alerts") is None
    pg.seed_declaration("alerts", description="the alerts state")
    row = await store.get_declaration("alerts")
    assert row is not None
    assert row["name"] == "alerts"
    assert row["description"] == "the alerts state"


async def test_list_declarations_ordered_by_name(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("zeta")
    pg.seed_declaration("alpha")
    pg.seed_declaration("mid")
    assert [r["name"] for r in await store.list_declarations()] == ["alpha", "mid", "zeta"]


async def test_upsert_declaration_inserts_then_updates(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await store.upsert_declaration("alerts", "d1", schema, ["thread"], "thread", None)
    # effective_schema defaults to the base schema when omitted
    row = pg.declarations["alerts"]
    assert row["effective_schema"] == schema
    assert row["subject_kinds"] == ["thread"]
    await store.upsert_declaration("alerts", "d2", schema, ["thread", "person"], "thread", 30)
    row = pg.declarations["alerts"]
    assert row["description"] == "d2"
    assert row["subject_kinds"] == ["thread", "person"]
    assert row["retention_days"] == 30
    assert len(pg.declarations) == 1  # updated in place, not duplicated


async def test_upsert_declaration_uses_explicit_effective_schema(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    base = {"type": "object", "properties": {"n": {"type": "integer"}}}
    effective = {"type": "object", "properties": {"n": {"type": "integer"}, "x": {"type": "string"}}}
    await store.upsert_declaration("alerts", "", base, ["thread"], "thread", None, effective_schema=effective)
    assert pg.declarations["alerts"]["effective_schema"] == effective


async def test_upsert_declaration_guarded_insert_calls_decide_with_no_existing(
    pg: FakeStatesPg, store: PostgresStatesStore
) -> None:
    seen: list[tuple] = []

    def decide(existing, per_kind):
        seen.append((existing, per_kind))

    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await store.upsert_declaration_guarded(
        "alerts", "", schema, ["thread"], "thread", None, effective_schema=schema, decide=decide
    )
    assert seen == [(None, {})]
    assert "alerts" in pg.declarations


async def test_upsert_declaration_guarded_reads_per_kind_counts_under_lock(
    pg: FakeStatesPg, store: PostgresStatesStore
) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    pg.seed_declaration("alerts", schema=schema, subject_kinds=["thread", "person"])
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    pg.seed_record("alerts", "agent", "a", "thread", "t2", {"n": 2})
    pg.seed_record("alerts", "agent", "a", "person", "p1", {"n": 3})
    seen: dict = {}

    def decide(existing, per_kind):
        seen.update(per_kind)

    await store.upsert_declaration_guarded(
        "alerts", "", schema, ["thread", "person"], "thread", None, effective_schema=schema, decide=decide
    )
    assert seen == {"thread": 2, "person": 1}


async def test_upsert_declaration_guarded_decide_raise_rolls_back(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    pg.seed_declaration("alerts", schema=schema, description="original")

    def decide(existing, per_kind):
        raise ValueError("refused")

    with pytest.raises(ValueError, match="refused"):
        await store.upsert_declaration_guarded(
            "alerts", "changed", schema, ["thread"], "thread", None, effective_schema=schema, decide=decide
        )
    # the txn rolled back — the description is untouched
    assert pg.declarations["alerts"]["description"] == "original"


async def test_delete_declaration_cascades_and_reports(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    pg.aliases[("alerts", "agent", "a", "thread", "old")] = {
        "state": "alerts",
        "target_kind": "agent",
        "target_name": "a",
        "alias_kind": "thread",
        "alias_key": "old",
        "canonical_kind": "thread",
        "canonical_key": "t1",
        "mode": "switch",
    }
    pg.writes.append(
        {
            "id": 1,
            "state": "alerts",
            "target_kind": "agent",
            "target_name": "a",
            "subject_kind": "thread",
            "subject_key": "t1",
            "seq": 1.0,
            "at": pg.tick(),
            "door": "api",
            "actor": None,
            "consumer": "c",
            "meta": None,
            "run_id": None,
            "turn_id": None,
            "paths": [[]],
            "op_id": None,
        }
    )
    assert await store.delete_declaration("alerts") is True
    assert pg.declarations == {}
    assert pg.records == {}
    assert pg.attachments == {}
    assert pg.aliases == {}
    assert pg.writes == []


async def test_delete_declaration_absent_returns_false(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    assert await store.delete_declaration("nope") is False


async def test_count_records(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    assert await store.count_records("alerts") == 0
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    pg.seed_record("alerts", "agent", "a", "thread", "t2", {"n": 2})
    assert await store.count_records("alerts") == 2


async def test_count_records_for_target(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_declaration("status")
    pg.seed_record("alerts", "tool", "weather", "thread", "t1", {"n": 1})
    pg.seed_record("status", "tool", "weather", "thread", "t1", {"n": 2})
    pg.seed_record("alerts", "tool", "echo", "thread", "t1", {"n": 3})
    assert await store.count_records_for_target("tool", "weather") == 2


async def test_field_stats(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1, "x": "y"})
    pg.seed_record("alerts", "agent", "a", "thread", "t2", {"n": 2})
    pg.seed_record("alerts", "agent", "a", "person", "p1", {"x": "z"})
    records, per_field, per_kind = await store.field_stats("alerts")
    assert records == 3
    assert per_field == {"n": 2, "x": 2}
    assert per_kind == {"thread": 2, "person": 1}


# --------------------------------------------------------------------------- #
# templates + attachments                                                              #
# --------------------------------------------------------------------------- #
async def test_template_upsert_get_list_delete(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    assert await store.get_template("m") is None
    await store.upsert_template("m", {"name": "m"}, "hash-1")
    row = await store.get_template("m")
    assert row is not None
    assert row["shipped_hash"] == "hash-1"
    await store.upsert_template("m", {"name": "m", "v": 2}, None)  # operator upload clears the hash
    updated = await store.get_template("m")
    assert updated is not None
    assert updated["shipped_hash"] is None
    await store.upsert_template("a", {"name": "a"}, None)
    assert [r["name"] for r in await store.list_templates()] == ["a", "m"]
    assert await store.delete_template("m") is True
    assert await store.delete_template("m") is False


async def test_attached_template_counts(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    for state, template in (("s1", "m1"), ("s2", "m1"), ("s1", "m2")):
        pg.attachments[(state, template)] = {
            "state": state,
            "template": template,
            "path": [],
            "parameters": {},
            "declarations": {},
            "updated_at": pg.tick(),
        }
    assert await store.attached_template_counts() == {"m1": 2, "m2": 1}


async def test_attach_reads(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    for state, template in (("s1", "m2"), ("s1", "m1"), ("s2", "m1")):
        pg.attachments[(state, template)] = {
            "state": state,
            "template": template,
            "path": ["p"],
            "parameters": {"k": 1},
            "declarations": {},
            "updated_at": pg.tick(),
        }
    assert await store.get_attachment("s1", "m1") is not None
    assert await store.get_attachment("s1", "nope") is None
    assert [r["template"] for r in await store.list_attachments_for_state("s1")] == ["m1", "m2"]
    assert [r["state"] for r in await store.list_attachments_of_template("m1")] == ["s1", "s2"]
    assert [(r["state"], r["template"]) for r in await store.list_all_attachments()] == [
        ("s1", "m1"),
        ("s1", "m2"),
        ("s2", "m1"),
    ]


async def test_upsert_attach_writes_row_and_effective_schema(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    eff = {"type": "object", "properties": {"a": {"type": "object"}}}
    await store.upsert_attachment("alerts", "m", ["a"], {"k": 1}, {"d": 2}, effective_schema=eff)
    assert pg.attachments[("alerts", "m")]["path"] == ["a"]
    assert pg.declarations["alerts"]["effective_schema"] == eff
    # a second upsert on the same (state, template) updates in place
    await store.upsert_attachment("alerts", "m", ["b"], {}, {}, effective_schema=eff)
    assert pg.attachments[("alerts", "m")]["path"] == ["b"]


async def test_upsert_attach_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.upsert_attachment("nope", "m", ["a"], {}, {}, effective_schema={})


async def test_update_attach_declarations(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {"old": 1},
        "updated_at": pg.tick(),
    }
    eff = {"type": "object", "properties": {"a": {"type": "object"}}}
    assert await store.update_attachment_declarations("alerts", "m", {"new": 2}, effective_schema=eff) is True
    assert pg.attachments[("alerts", "m")]["declarations"] == {"new": 2}
    assert pg.declarations["alerts"]["effective_schema"] == eff
    # no such attach → False (and the declaration lock passed since the state exists)
    assert await store.update_attachment_declarations("alerts", "absent", {}, effective_schema=eff) is False


async def test_update_attach_declarations_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.update_attachment_declarations("nope", "m", {}, effective_schema={})


async def test_update_attach_parameters(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {"k": 1},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    eff = {"type": "object", "properties": {"a": {"type": "object"}}}
    assert await store.update_attachment_parameters("alerts", "m", {"k": 2}, effective_schema=eff) is True
    assert pg.attachments[("alerts", "m")]["parameters"] == {"k": 2}
    assert await store.update_attachment_parameters("alerts", "absent", {}, effective_schema=eff) is False


async def test_update_attach_parameters_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.update_attachment_parameters("nope", "m", {}, effective_schema={})


async def test_delete_attach(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    eff = {"type": "object", "properties": {}}
    assert await store.delete_attachment("alerts", "m", effective_schema=eff) is True
    assert ("alerts", "m") not in pg.attachments
    assert pg.declarations["alerts"]["effective_schema"] == eff
    assert await store.delete_attachment("alerts", "m", effective_schema=eff) is False


async def test_delete_attach_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.delete_attachment("nope", "m", effective_schema={})


# --------------------------------------------------------------------------- #
# record reads                                                                  #
# --------------------------------------------------------------------------- #
async def test_read_record_hit_and_miss(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    assert await store.read_record("alerts", _subj()) == (None, None)
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    data, seq = await store.read_record("alerts", _subj())
    assert data == {"n": 1}
    assert seq is not None


async def test_read_record_resolves_alias(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "new", {"n": 9})
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
    data, _seq = await store.read_record("alerts", _subj(key="old"))
    assert data == {"n": 9}


async def test_read_record_view_reports_folded_from(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "new", {"n": 9})
    for old in ("b-old", "a-old"):
        pg.aliases[("alerts", "agent", "a", "thread", old)] = {
            "state": "alerts",
            "target_kind": "agent",
            "target_name": "a",
            "alias_kind": "thread",
            "alias_key": old,
            "canonical_kind": "thread",
            "canonical_key": "new",
            "mode": "switch",
        }
    view = await store.read_record_view("alerts", _subj(key="a-old"))
    assert view is not None
    assert view["canonical_subject"].key == "new"
    assert [s.key for s in view["folded_from"]] == ["a-old", "b-old"]
    assert await store.read_record_view("alerts", _subj(key="ghost")) is None


async def test_export_records_and_list_aliases(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "b", "thread", "t1", {"n": 2})
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    exported = await store.export_records("alerts")
    assert [(r["target_name"], r["data"]) for r in exported] == [("a", {"n": 1}), ("b", {"n": 2})]
    pg.aliases[("alerts", "agent", "a", "thread", "old")] = {
        "state": "alerts",
        "target_kind": "agent",
        "target_name": "a",
        "alias_kind": "thread",
        "alias_key": "old",
        "canonical_kind": "thread",
        "canonical_key": "t1",
        "mode": "merge",
    }
    aliases = await store.list_aliases("alerts")
    assert aliases[0]["alias_key"] == "old"
    assert aliases[0]["mode"] == "merge"


# --------------------------------------------------------------------------- #
# record writes: replace / apply_ops / erase / fold                            #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# listing / search / writes paging                                             #
# --------------------------------------------------------------------------- #
async def test_list_subjects_keyset_paging(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "same", {"n": 1})
    pg.seed_record("alerts", "agent", "b", "thread", "same", {"n": 2})
    listed: list[tuple[str, str]] = []
    cursor: str | None = None
    while True:
        rows = await store.list_subjects("alerts", kind=None, limit=1, cursor=cursor)
        if not rows:
            break
        listed.extend((r["target_name"], r["subject_key"]) for r in rows)
        last = rows[-1]
        cursor = make_cursor(last["target_kind"], last["target_name"], last["subject_kind"], last["subject_key"])
    assert listed == [("a", "same"), ("b", "same")]


async def test_list_subjects_filtered_by_kind(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts", subject_kinds=["thread", "person"])
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    pg.seed_record("alerts", "agent", "a", "person", "p1", {"n": 2})
    rows = await store.list_subjects("alerts", kind="person", limit=10, cursor=None)
    assert [r["subject_key"] for r in rows] == ["p1"]


async def test_search_records_containment(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1, "tag": "x"})
    pg.seed_record("alerts", "agent", "a", "thread", "t2", {"n": 2, "tag": "y"})
    rows = await store.search_records("alerts", {"tag": "x"}, limit=10, cursor=None)
    assert [r["subject_key"] for r in rows] == ["t1"]


async def test_writes_paging_newest_first_and_alias_aware(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    for i in range(3):
        await store.replace("alerts", _subj(), {"n": i}, origin=_ORIGIN, validate_doc=_ok)
    first = await store.writes("alerts", _subj(), limit=2, cursor=None)
    assert len(first) == 2
    assert first[0]["id"] > first[1]["id"]  # newest first
    nxt = await store.writes("alerts", _subj(), limit=2, cursor=str(first[-1]["id"]))
    assert len(nxt) == 1
    assert nxt[0]["id"] < first[-1]["id"]


async def test_writes_follows_a_fold(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    s1, s2 = _subj(key="old"), _subj(key="new")
    await store.replace("alerts", s2, {"n": 2}, origin=_ORIGIN, validate_doc=_ok)
    await store.fold_subject("alerts", s1, s2, "switch", origin=_ORIGIN, validate_doc=_ok)
    # the old key's audit trail resolves onto the survivor's writes
    rows = await store.writes("alerts", s1, limit=10, cursor=None)
    assert rows  # the survivor's replace + the fold write row are visible through the old key


# --------------------------------------------------------------------------- #
# restore paths + retention                                                     #
# --------------------------------------------------------------------------- #
async def test_restore_records_validates_and_audits(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    rows = [
        {"target_kind": "agent", "target_name": "a", "subject_kind": "thread", "subject_key": "t1", "data": {"n": 1}},
        {"target_kind": "agent", "target_name": "a", "subject_kind": "thread", "subject_key": "t2", "data": {"n": 2}},
    ]
    await store.restore_records("alerts", rows, origin=_ORIGIN, validate_doc=_ok)
    assert (await store.read_record("alerts", _subj(key="t1")))[0] == {"n": 1}
    assert len([w for w in pg.writes if w["paths"] == [[]]]) == 2


async def test_restore_records_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.restore_records("nope", [], origin=_ORIGIN, validate_doc=_ok)


async def test_restore_aliases_upserts(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    rows = [
        {
            "target_kind": "agent",
            "target_name": "a",
            "alias_kind": "thread",
            "alias_key": "old",
            "canonical_kind": "thread",
            "canonical_key": "new",
            "mode": "switch",
        }
    ]
    await store.restore_aliases("alerts", rows)
    assert pg.aliases[("alerts", "agent", "a", "thread", "old")]["canonical_key"] == "new"
    # a second restore of the same alias key overwrites (ON CONFLICT DO UPDATE), never errors
    rows[0]["canonical_key"] = "newer"
    await store.restore_aliases("alerts", rows)
    assert pg.aliases[("alerts", "agent", "a", "thread", "old")]["canonical_key"] == "newer"


async def test_prune_ops(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    from datetime import timedelta

    pg.applied_ops["stale"] = pg.now() - timedelta(days=40)
    pg.applied_ops["fresh"] = pg.now()
    await store.prune_ops(30)
    assert "stale" not in pg.applied_ops
    assert "fresh" in pg.applied_ops


async def test_prune_expired_honors_per_state_and_default(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    from datetime import timedelta

    old = pg.now() - timedelta(days=100)
    young = pg.now() - timedelta(days=30)
    # a state with its own short retention → its old record expires
    pg.seed_declaration("shortlived", retention_days=10)
    pg.seed_record("shortlived", "agent", "a", "thread", "t1", {"n": 1}, updated_at=old)
    # a state relying on the global default (50 days): the 100-day record expires under it,
    # the 30-day one survives — the COALESCE(per-state, default) predicate on each row
    pg.seed_declaration("defaulted", retention_days=None)
    pg.seed_record("defaulted", "agent", "a", "thread", "old", {"n": 1}, updated_at=old)
    pg.seed_record("defaulted", "agent", "a", "thread", "young", {"n": 2}, updated_at=young)
    counts = await store.prune_expired(50)  # global default 50 days
    assert counts == {"shortlived": 1, "defaulted": 1}
    assert ("defaulted", "agent", "a", "thread", "young") in pg.records


async def test_prune_expired_none_default_keeps_all(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    from datetime import timedelta

    pg.seed_declaration("forever", retention_days=None)
    pg.seed_record("forever", "agent", "a", "thread", "t1", {"n": 1}, updated_at=pg.now() - timedelta(days=999))
    assert await store.prune_expired(None) == {}
    assert len(pg.records) == 1
