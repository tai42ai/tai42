"""A write through an alias onto a ``person`` record takes that person's gate and admits that person.

Driven against the in-memory fake Postgres (the ``pg``/``store`` fixtures in ``conftest``): a thread
folded into a person resolves to the person, so every write path that resolves the alias gates and
admits the person it writes, in the same transaction, like a direct write on the person.
"""

from __future__ import annotations

from tai42_contract.states.models import CompletedOrigin, StateSubject

from tai42_skeleton.states.service.catalog import CatalogSnapshot
from tai42_skeleton.states.store import PostgresStatesStore
from tai42_skeleton.states.store.person_gate import person_gate_key

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

_THREAD = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
_PERSON = StateSubject(target_kind="agent", target_name="a", kind="person", key="p1")
_OTHER_THREAD = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t2")


class _Admissions:
    """Records every in-transaction admission with the person gates held when it ran."""

    def __init__(self, pg: FakeStatesPg) -> None:
        self._pg = pg
        self.seen: list[tuple[StateSubject, list[str]]] = []

    async def __call__(self, subject_kinds: list[str], canonical: StateSubject) -> None:
        self.seen.append((canonical, list(self._pg.shared_advisory_locks)))


async def _thread_folded_into_person(
    pg: FakeStatesPg, store: PostgresStatesStore, catalog: CatalogSnapshot
) -> _Admissions:
    pg.seed_declaration("alerts", subject_kinds=["thread", "person"])
    admit = _Admissions(pg)
    await store.replace("alerts", _PERSON, {"n": 1}, origin=_ORIGIN, catalog=catalog, validate_subject_in_txn=admit)
    await store.fold_subject(
        "alerts", _THREAD, _PERSON, "switch", origin=_ORIGIN, catalog=catalog, validate_subjects_in_txn=admit
    )
    pg.shared_advisory_locks.clear()
    admit.seen.clear()
    return admit


async def test_a_replace_through_an_alias_gates_and_admits_the_person_it_writes(
    pg: FakeStatesPg, store: PostgresStatesStore, catalog: CatalogSnapshot
) -> None:
    admit = await _thread_folded_into_person(pg, store, catalog)
    await store.replace("alerts", _THREAD, {"n": 2}, origin=_ORIGIN, catalog=catalog, validate_subject_in_txn=admit)
    assert admit.seen == [(_PERSON, [person_gate_key(_PERSON)])]
    assert (await store.read_record("alerts", _PERSON))[0] == {"n": 2}


async def test_an_apply_through_an_alias_gates_and_admits_the_person_it_writes(
    pg: FakeStatesPg, store: PostgresStatesStore, catalog: CatalogSnapshot
) -> None:
    admit = await _thread_folded_into_person(pg, store, catalog)
    ops = [{"op": "set", "path": ["n"], "value": 3}]
    applied, *_ = await store.apply_ops(
        "alerts", _THREAD, ops, op_id="op-1", origin=_ORIGIN, catalog=catalog, validate_subject_in_txn=admit
    )
    assert applied is True
    assert admit.seen == [(_PERSON, [person_gate_key(_PERSON)])]


async def test_a_fold_into_an_alias_gates_and_admits_the_person_it_writes(
    pg: FakeStatesPg, store: PostgresStatesStore, catalog: CatalogSnapshot
) -> None:
    admit = await _thread_folded_into_person(pg, store, catalog)
    await store.replace(
        "alerts", _OTHER_THREAD, {"n": 4}, origin=_ORIGIN, catalog=catalog, validate_subject_in_txn=admit
    )
    admit.seen.clear()
    pg.shared_advisory_locks.clear()
    await store.fold_subject(
        "alerts", _OTHER_THREAD, _THREAD, "merge", origin=_ORIGIN, catalog=catalog, validate_subjects_in_txn=admit
    )
    assert admit.seen == [(_PERSON, [person_gate_key(_PERSON)])]


async def test_a_write_on_an_unaliased_subject_is_admitted_as_itself(
    pg: FakeStatesPg, store: PostgresStatesStore, catalog: CatalogSnapshot
) -> None:
    pg.seed_declaration("alerts", subject_kinds=["thread", "person"])
    admit = _Admissions(pg)
    await store.replace("alerts", _THREAD, {"n": 1}, origin=_ORIGIN, catalog=catalog, validate_subject_in_txn=admit)
    assert admit.seen == [(_THREAD, [])]
