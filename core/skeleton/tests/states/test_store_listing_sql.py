"""The keyset-paged subject/record listing, containment search, and write-ledger paging SQL —
driven against the in-memory fake Postgres (the ``pg``/``store`` fixtures in ``conftest``).
"""

from __future__ import annotations

from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.models import CompletedOrigin, StateSubject

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
