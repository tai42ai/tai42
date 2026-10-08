"""The door binding and the builtin state tools resolve a subject through one platform resolver.

Per reference form, with and without an ambient context, a binding's ``subject_expr`` and a state
tool's ``subject`` argument reach the same verdict: the same resolved subject or the same refusal.
Driven against the real :class:`StatesService` over the in-memory Postgres, served by the real
:class:`StatesFacet`, on a neutral synthetic state ``probe-state``.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.states import StateAttach, StateContext, StateSubject, SubjectCandidates
from tai42_contract.states.errors import StateNotFoundError, SubjectRefusedError
from tai42_contract.states.models import StateDeclaration
from tai42_contract.template import TemplatedText

from tai42_skeleton.app.facets.states import StatesFacet
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService, state_context
from tai42_skeleton.states.store import PostgresStatesStore
from tai42_skeleton.tools.builtin import states as builtin_states
from tai42_skeleton.tools.state_binding import _resolve_subject as binding_subject

from ...states.conftest import FakeStatesPg, pg  # noqa: F401  (the in-memory Postgres fixture)

_CTX = StateContext(
    door="hook",
    candidates=SubjectCandidates(target_kind="agent", target_name="relay", by_kind={"thread": "t-1"}),
)


@pytest.fixture
async def facet(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch) -> StatesFacet:  # noqa: F811
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


class _RecordingStates:
    """The real facet, recording the subject every ``read`` addresses."""

    def __init__(self, facet: StatesFacet) -> None:
        self._facet = facet
        self.read_subjects: list[StateSubject] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._facet, name)

    async def read(self, state: str, subject: StateSubject) -> Any:
        self.read_subjects.append(subject)
        return await self._facet.read(state, subject)


class _InlineManager:
    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        assert text.content is not None
        return text.content


async def _verdict(call: Any) -> Any:
    try:
        return await call()
    except (SubjectRefusedError, StateNotFoundError) as exc:
        return (type(exc).__name__, str(exc))


@pytest.mark.parametrize("with_context", [True, False])
@pytest.mark.parametrize(
    "ref",
    [
        {"target_kind": "tool", "target_name": "probe", "kind": "session", "key": "s-1"},
        {"kind": "session", "key": "s-7"},
        {"target_kind": "agent", "kind": "thread", "key": "k"},
        {"target_kind": "agent", "target_name": "relay", "kind": "thread"},
        {"kind": "Bad Kind", "key": "k"},
        {"key": "k"},
    ],
)
async def test_a_binding_and_a_state_tool_give_the_same_verdict_per_reference(
    facet: StatesFacet, bind_app: Any, ref: dict[str, Any], with_context: bool
) -> None:
    states = _RecordingStates(facet)
    app = SimpleNamespace(states=states, storage=SimpleNamespace(resource_manager=_InlineManager()))
    bind_app(app)
    attach = StateAttach(state="probe-state", subject_expr=TemplatedText(content=json.dumps(ref)))

    async def through_binding() -> Any:
        return await binding_subject(app, attach, {})  # type: ignore[arg-type]

    async def through_tool() -> Any:
        await builtin_states.state_read("probe-state", subject=ref)
        return states.read_subjects[-1]

    with state_context(_CTX) if with_context else nullcontext():
        on_binding = await _verdict(through_binding)
        on_tool = await _verdict(through_tool)
    assert on_binding == on_tool


_FULL = {"target_kind": "tool", "target_name": "probe", "kind": "session", "key": "s-1"}


@pytest.mark.parametrize("ref", [_FULL, {"kind": "session", "key": "s-7"}])
async def test_a_binding_resolves_a_mapping_subject_with_no_store_statement(
    facet: StatesFacet,
    pg: FakeStatesPg,  # noqa: F811
    bind_app: Any,
    ref: dict[str, Any],
) -> None:
    app = SimpleNamespace(states=facet, storage=SimpleNamespace(resource_manager=_InlineManager()))
    bind_app(app)
    attach = StateAttach(state="probe-state", subject_expr=TemplatedText(content=json.dumps(ref)))
    pg.executed.clear()
    with state_context(_CTX):
        await binding_subject(app, attach, {})  # type: ignore[arg-type]
    assert pg.executed == []


@pytest.mark.parametrize("ref", [_FULL, {"kind": "session", "key": "s-7"}])
async def test_a_state_tool_addressing_a_mapping_subject_costs_only_its_record_door(
    facet: StatesFacet,
    pg: FakeStatesPg,  # noqa: F811
    bind_app: Any,
    ref: dict[str, Any],
) -> None:
    bind_app(SimpleNamespace(states=facet))
    with state_context(_CTX):
        subject = await facet.resolve_subject("probe-state", ref)
        await facet.read("probe-state", subject)
        pg.executed.clear()
        await facet.read("probe-state", subject)
        record_door = [sql for sql, _ in pg.executed]
        pg.executed.clear()
        await builtin_states.state_read("probe-state", subject=ref)
    assert [sql for sql, _ in pg.executed] == record_door
