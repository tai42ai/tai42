"""The backup restore SQL (records validated-and-audited, aliases upserted) and the retention
sweep SQL (op-ledger prune, per-state/default expired-record prune) — driven against the
in-memory fake Postgres (the ``pg``/``store`` fixtures in ``conftest``).
"""

from __future__ import annotations

import pytest
from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.errors import StateNotFoundError
from tai42_contract.states.models import CompletedOrigin, StateSubject

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
    removed = await store.prune_ops(30)
    assert removed == 1
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
