"""The one subject resolver: ``resolve_subject`` on the states facet, per reference form.

A neutral synthetic state ``probe-state`` (subject kinds ``thread``/``session``, default ``thread``)
read through the real :class:`StatesService` over the in-memory Postgres and served by the real
:class:`StatesFacet`; the ambient context is deposited with :func:`state_context` as a door does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.states import StateContext, StateSubject, SubjectCandidates
from tai42_contract.states.errors import StateNotFoundError, SubjectRefusedError
from tai42_contract.states.models import StateDeclaration

from tai42_skeleton.app.facets.states import StatesFacet
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService, state_context
from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg

_CTX = StateContext(
    door="hook",
    candidates=SubjectCandidates(target_kind="agent", target_name="relay", by_kind={"thread": "t-1"}),
)


def _subject(kind: str, key: str, *, target_kind: str = "agent", target_name: str = "relay") -> StateSubject:
    return StateSubject.model_validate(
        {"target_kind": target_kind, "target_name": target_name, "kind": kind, "key": key}
    )


@pytest.fixture
async def facet(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch) -> StatesFacet:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    svc = StatesService(store=PostgresStatesStore())
    await svc.put_declaration(
        StateDeclaration(
            name="probe-state",
            schema={"type": "object", "properties": {"n": {}}},
            subject_kinds=["thread", "session"],
            default_subject_kind="thread",
        )
    )
    return StatesFacet(SimpleNamespace(_states_service=svc))  # type: ignore[arg-type]


async def test_a_full_mapping_is_the_subject_with_or_without_context(facet: StatesFacet) -> None:
    ref = {"target_kind": "tool", "target_name": "probe", "kind": "session", "key": "s-1"}
    expected = _subject("session", "s-1", target_kind="tool", target_name="probe")
    assert await facet.resolve_subject("probe-state", ref) == expected
    with state_context(_CTX):
        assert await facet.resolve_subject("probe-state", ref) == expected


async def test_a_kind_key_mapping_takes_its_target_from_the_context(facet: StatesFacet) -> None:
    with state_context(_CTX):
        resolved = await facet.resolve_subject("probe-state", {"kind": "session", "key": "s-7"})
    assert resolved == _subject("session", "s-7")


async def test_a_key_string_takes_the_default_kind_and_the_context_target(facet: StatesFacet) -> None:
    with state_context(_CTX):
        resolved = await facet.resolve_subject("probe-state", "k-9")
    assert resolved == _subject("thread", "k-9")


async def test_none_takes_the_ambient_candidate_of_the_default_kind(facet: StatesFacet) -> None:
    with state_context(_CTX):
        resolved = await facet.resolve_subject("probe-state", None)
    assert resolved == _subject("thread", "t-1")


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        (
            {"target_kind": "agent", "kind": "thread", "key": "k"},
            "state 'probe-state': an explicit subject must give both target_kind and target_name or neither",
        ),
        (
            {"target_name": "relay", "kind": "thread", "key": "k"},
            "state 'probe-state': an explicit subject must give both target_kind and target_name or neither",
        ),
        (
            {"kind": "thread", "key": "k"},
            "state 'probe-state': subject {'kind': 'thread', 'key': 'k'} names no target and no ambient context "
            "is in scope to supply one — give target_kind and target_name",
        ),
        (
            "k",
            "state 'probe-state': subject 'k' names no target and no ambient context is in scope to supply one "
            "— give target_kind and target_name",
        ),
        (None, "state 'probe-state': no subject in scope: pass subject explicitly"),
    ],
)
async def test_a_reference_needing_a_context_is_refused_without_one(facet: StatesFacet, ref: Any, message: str) -> None:
    with pytest.raises(SubjectRefusedError) as excinfo:
        await facet.resolve_subject("probe-state", ref)
    assert str(excinfo.value) == message


@pytest.mark.parametrize("ref", ["", "   "])
async def test_a_blank_key_is_refused(facet: StatesFacet, ref: str) -> None:
    with state_context(_CTX), pytest.raises(SubjectRefusedError) as excinfo:
        await facet.resolve_subject("probe-state", ref)
    assert str(excinfo.value) == f"state 'probe-state': a subject key must be a non-empty string, got {ref!r}"


async def test_a_missing_ambient_candidate_is_refused_naming_kind_and_door(facet: StatesFacet) -> None:
    ctx = StateContext(
        door="schedule",
        candidates=SubjectCandidates(target_kind="agent", target_name="relay", by_kind={"session": "s-1"}),
    )
    with state_context(ctx), pytest.raises(SubjectRefusedError) as excinfo:
        await facet.resolve_subject("probe-state", None)
    assert str(excinfo.value) == (
        "state 'probe-state': the ambient 'schedule' door resolved no subject of kind 'thread' "
        "(the state's default_subject_kind) — pass subject explicitly"
    )


@pytest.mark.parametrize(
    ("ref", "field"),
    [
        ({"target_kind": "nowhere", "target_name": "relay", "kind": "thread", "key": "k"}, "target_kind"),
        ({"target_kind": "agent", "target_name": "relay", "kind": "thread", "key": " "}, "key"),
        ({"target_kind": "agent", "target_name": "relay", "kind": "thread"}, "key"),
        ({"target_kind": "agent", "target_name": "relay", "kind": "thread", "key": "k", "x": 1}, "x"),
        ({"kind": "Bad Kind", "key": "k"}, "kind"),
        ({"key": "k"}, "kind"),
    ],
)
async def test_a_malformed_mapping_is_refused_naming_the_field(facet: StatesFacet, ref: Any, field: str) -> None:
    with state_context(_CTX), pytest.raises(SubjectRefusedError) as excinfo:
        await facet.resolve_subject("probe-state", ref)
    message = str(excinfo.value)
    assert message.startswith(f"state 'probe-state': subject {ref!r} is not a valid subject: {field}: ")


@pytest.mark.parametrize("ref", [7, ["thread", "k"], True])
async def test_any_other_shape_is_refused(facet: StatesFacet, ref: Any) -> None:
    with state_context(_CTX), pytest.raises(SubjectRefusedError) as excinfo:
        await facet.resolve_subject("probe-state", ref)
    assert str(excinfo.value) == (
        f"state 'probe-state': a subject is a subject object, a key string or omitted, got {ref!r}"
    )


@pytest.mark.parametrize("ref", [None, "k"])
async def test_a_form_completed_from_the_declaration_on_an_undeclared_state_is_not_found(
    facet: StatesFacet, ref: Any
) -> None:
    with state_context(_CTX), pytest.raises(StateNotFoundError, match="no state declared as 'ghost'"):
        await facet.resolve_subject("ghost", ref)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ({"kind": "thread", "key": "k"}, _subject("thread", "k")),
        (
            {"target_kind": "tool", "target_name": "probe", "kind": "thread", "key": "k"},
            _subject("thread", "k", target_kind="tool", target_name="probe"),
        ),
    ],
)
async def test_a_mapping_on_an_undeclared_state_resolves_and_its_record_door_refuses_it(
    facet: StatesFacet, ref: Any, expected: StateSubject
) -> None:
    with state_context(_CTX):
        resolved = await facet.resolve_subject("ghost", ref)
        assert resolved == expected
        with pytest.raises(StateNotFoundError, match="no state declared as 'ghost'"):
            await facet.read("ghost", resolved)


@pytest.mark.parametrize(
    "ref",
    [
        {"target_kind": "tool", "target_name": "probe", "kind": "session", "key": "s-1"},
        {"kind": "session", "key": "s-7"},
    ],
)
async def test_a_mapping_resolves_with_no_store_statement(facet: StatesFacet, pg: FakeStatesPg, ref: Any) -> None:
    pg.executed.clear()
    with state_context(_CTX):
        await facet.resolve_subject("probe-state", ref)
    assert pg.executed == []


@pytest.mark.parametrize(
    "ref",
    [
        {"target_kind": "agent", "kind": "thread", "key": "k"},
        {"kind": "Bad Kind", "key": "k"},
    ],
)
async def test_a_malformed_mapping_is_refused_with_no_store_statement(
    facet: StatesFacet, pg: FakeStatesPg, ref: Any
) -> None:
    pg.executed.clear()
    with state_context(_CTX), pytest.raises(SubjectRefusedError):
        await facet.resolve_subject("probe-state", ref)
    assert pg.executed == []


@pytest.mark.parametrize("ref", [None, "k-9"])
async def test_a_key_string_or_an_omitted_subject_reads_the_declaration_once(
    facet: StatesFacet, pg: FakeStatesPg, ref: Any
) -> None:
    pg.executed.clear()
    with state_context(_CTX):
        await facet.resolve_subject("probe-state", ref)
    assert [sql for sql, _ in pg.executed] == [
        "SELECT version, subject_kinds, default_subject_kind FROM state_declarations WHERE name = %s"
    ]
