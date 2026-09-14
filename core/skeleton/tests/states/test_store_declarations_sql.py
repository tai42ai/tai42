"""The ``state_declarations`` SQL — reads, guarded upsert, cascade delete, and record
counts/field stats — driven against the in-memory fake Postgres (the ``pg``/``store``
fixtures in ``conftest``), so each method's statement shape runs through its true SQL with no
live database.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg


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
