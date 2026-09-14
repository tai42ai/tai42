"""The alias-aware ``state_records`` read SQL — single-record and view reads (with
``folded_from``) and the backup exporter's record/alias reads — driven against the in-memory
fake Postgres (the ``pg``/``store`` fixtures in ``conftest``).
"""

from __future__ import annotations

from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.models import StateSubject

from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg


def _subj(key="t1", kind="thread", tk: ConversationTargetKind = "agent", tn="a"):
    return StateSubject(target_kind=tk, target_name=tn, kind=kind, key=key)


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
