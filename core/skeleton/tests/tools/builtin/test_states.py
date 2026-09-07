"""The builtin subject-state tools: subject resolution per door context, the explicit
subject override, the loud refusal when nothing is in scope, and the write-provenance
discipline (the tool supplies a consumer-only origin; the platform stamps
door/actor/turn_id from the deposited context).

The tools are driven against a real :class:`StatesService` over an in-memory fake store
bound as ``tai42_app.states`` — so each assertion covers the COMPOSED path
(door context → tool → facet chokepoint → store), not a re-implemented stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.utilities.types import get_cached_typeadapter
from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.monitoring import RunAttribution
from tai42_contract.states import (
    StateContext,
    StateNotFoundError,
    StateSubject,
    SubjectCandidates,
    SubjectRefusedError,
)
from tai42_contract.tools import (
    ToolInvocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService, state_context
from tai42_skeleton.tools.attribution import run_attribution
from tai42_skeleton.tools.builtin import states as builtin_states


class _FakeStore:
    """An in-memory stand-in for :class:`PostgresStatesStore` covering the record methods
    the service drives from the state tools (read / replace / apply / writes) plus the
    mount lookup its effective-schema composition reads."""

    def __init__(self) -> None:
        self.declarations: dict[str, dict[str, Any]] = {}
        self.mounts: dict[tuple[str, str], dict[str, Any]] = {}
        self.records: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.write_rows: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        self._seq = 0.0

    @staticmethod
    def _key(state: str, subject: StateSubject) -> tuple[str, str, str, str, str]:
        return (state, subject.target_kind, subject.target_name, subject.kind, subject.key)

    def declare(self, decl: dict[str, Any]) -> None:
        self.declarations[decl["name"]] = decl

    async def get_declaration(self, name: str) -> dict[str, Any] | None:
        return self.declarations.get(name)

    async def list_mounts_for_state(self, state: str) -> list[dict[str, Any]]:
        return [row for (mounted_state, _module), row in self.mounts.items() if mounted_state == state]

    async def read_record_view(self, state: str, subject: StateSubject) -> dict[str, Any] | None:
        row = self.records.get(self._key(state, subject))
        if row is None:
            return None
        return {"data": row["data"], "seq": row["seq"], "canonical_subject": subject, "folded_from": []}

    async def replace(self, state, subject, data, *, origin, validate_doc) -> None:
        self._seq += 1
        self.records[self._key(state, subject)] = {"data": dict(data), "seq": self._seq}
        self._append_write(state, subject, origin, [[]])

    async def apply_ops(self, state, subject, ops, *, op_id, origin, validate_doc, retention_days):
        self._seq += 1
        key = self._key(state, subject)
        doc = dict(self.records.get(key, {"data": {}})["data"])
        paths: list[list[Any]] = []
        for op in ops:
            paths.append(list(op["path"]))
            if op["op"] == "set":
                target = doc
                for segment in op["path"][:-1]:
                    target = target.setdefault(segment, {})
                target[op["path"][-1]] = op["value"]
        self.records[key] = {"data": doc, "seq": self._seq}
        self._append_write(state, subject, origin, paths)
        return (True, doc, self._seq, [])

    def _append_write(self, state, subject, origin, paths) -> None:
        self.write_rows.setdefault(self._key(state, subject), []).append(
            {
                "seq": self._seq,
                "at": datetime.now(UTC),
                "consumer": origin.consumer,
                "meta": origin.meta,
                "run_id": origin.run_id,
                "op_id": origin.op_id,
                "door": origin.door,
                "actor": origin.actor,
                "turn_id": origin.turn_id,
                "paths": paths,
            }
        )

    async def writes(self, state, subject, *, limit, cursor):
        rows = list(reversed(self.write_rows.get(self._key(state, subject), [])))
        return rows[:limit]


def _declaration(
    name: str = "notes",
    *,
    subject_kinds: list[str] | None = None,
    default_subject_kind: str = "thread",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": "",
        "schema": {"type": "object"},
        "subject_kinds": subject_kinds or ["thread", "session"],
        "default_subject_kind": default_subject_kind,
        "retention_days": None,
        "effective_schema": {"type": "object"},
    }


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    # The autouse conftest fixture turns the states gate OFF; re-enable it so the
    # service serves rather than raising 501 in these unit tests.
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    service = StatesService(store=_FakeStore())  # type: ignore[arg-type]
    service._store.declare(_declaration())  # type: ignore[attr-defined]
    return service


@pytest.fixture
def app(bind_app, svc: StatesService):  # type: ignore[no-untyped-def]
    """Bind a fake ``tai42_app`` whose ``states`` facet is the real service."""
    return bind_app(SimpleNamespace(states=svc))


def _context(
    door: str = "conversation",
    *,
    by_kind: dict[str, str] | None = None,
    target_kind: ConversationTargetKind = "agent",
    target_name: str = "a",
    actor: str | None = None,
    turn_id: str | None = None,
    inbound_id: str | None = None,
) -> StateContext:
    return StateContext(
        door=door,  # type: ignore[arg-type]
        candidates=SubjectCandidates(target_kind=target_kind, target_name=target_name, by_kind=by_kind or {}),
        actor=actor,
        turn_id=turn_id,
        inbound_id=inbound_id,
    )


def _store(svc: StatesService) -> _FakeStore:
    return svc._store  # type: ignore[return-value]


async def test_ambient_resolution_from_conversation_door(app, svc: StatesService) -> None:
    with state_context(_context(by_kind={"thread": "t-1"})):
        await builtin_states.state_merge("notes", {"n": 1})
    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t-1")
    view = await svc.read("notes", subject)
    assert view is not None
    assert view.data == {"n": 1}


async def test_ambient_resolution_picks_default_subject_kind(app, svc: StatesService) -> None:
    # The ambient key is the candidate for the state's default_subject_kind — NOT just
    # any kind the door knows.
    _store(svc).declare(_declaration(default_subject_kind="session"))
    with state_context(_context(by_kind={"thread": "t-1", "session": "sess-9"})):
        await builtin_states.state_merge("notes", {"n": 2})
    on_session = await svc.read(
        "notes", StateSubject(target_kind="agent", target_name="a", kind="session", key="sess-9")
    )
    on_thread = await svc.read("notes", StateSubject(target_kind="agent", target_name="a", kind="thread", key="t-1"))
    assert on_session is not None
    assert on_session.data == {"n": 2}
    assert on_thread is None


async def test_explicit_full_subject_overrides_context(app, svc: StatesService) -> None:
    with state_context(_context(by_kind={"thread": "t-1"})):
        await builtin_states.state_merge(
            "notes",
            {"n": 3},
            subject={"target_kind": "tool", "target_name": "flowX", "kind": "session", "key": "sess-explicit"},
        )
    # written under the explicit subject, not the ambient thread one
    explicit = await svc.read(
        "notes", StateSubject(target_kind="tool", target_name="flowX", kind="session", key="sess-explicit")
    )
    ambient = await svc.read("notes", StateSubject(target_kind="agent", target_name="a", kind="thread", key="t-1"))
    assert explicit is not None
    assert explicit.data == {"n": 3}
    assert ambient is None


async def test_explicit_kind_key_takes_target_from_context(app, svc: StatesService) -> None:
    with state_context(_context(target_kind="agent", target_name="a", by_kind={"thread": "t-1"})):
        await builtin_states.state_merge("notes", {"n": 4}, subject={"kind": "session", "key": "s-7"})
    view = await svc.read("notes", StateSubject(target_kind="agent", target_name="a", kind="session", key="s-7"))
    assert view is not None
    assert view.data == {"n": 4}


async def test_explicit_kind_key_without_target_and_no_context_refused(app) -> None:
    with pytest.raises(SubjectRefusedError, match="names no target"):
        await builtin_states.state_read("notes", subject={"kind": "session", "key": "s-7"})


async def test_no_subject_and_no_context_refused(app) -> None:
    with pytest.raises(SubjectRefusedError, match="no subject in scope"):
        await builtin_states.state_read("notes")


async def test_default_kind_missing_from_candidates_names_state_kind_door(app) -> None:
    # The default kind is 'thread' but the hook door resolved no thread candidate — the
    # refusal names the state, the kind, and the door.
    with (
        state_context(_context(door="hook", by_kind={"session": "s-1"})),
        pytest.raises(SubjectRefusedError) as excinfo,
    ):
        await builtin_states.state_merge("notes", {"n": 5})
    message = str(excinfo.value)
    assert "notes" in message
    assert "thread" in message
    assert "hook" in message


async def test_ambient_resolution_undeclared_state_raises_not_found(app) -> None:
    with state_context(_context(by_kind={"thread": "t-1"})), pytest.raises(StateNotFoundError, match="ghost"):
        await builtin_states.state_read("ghost")


async def test_state_read_returns_none_when_absent(app) -> None:
    with state_context(_context(by_kind={"thread": "t-1"})):
        assert await builtin_states.state_read("notes") is None


async def test_origin_is_consumer_only_writes_row_stamped_by_platform(app, svc: StatesService) -> None:
    invocation_token = set_current_tool_invocation(ToolInvocation(tool_name="state_apply"))
    ctx = _context(actor="user-7", turn_id="turn-9", inbound_id="inb-3", by_kind={"thread": "t-1"})
    try:
        with run_attribution(RunAttribution(session_id="sess-42")), state_context(ctx):
            await builtin_states.state_apply("notes", [{"op": "set", "path": ["n"], "value": 9}], op_id="op-1")
    finally:
        reset_current_tool_invocation(invocation_token)

    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t-1")
    entries = (await svc.writes("notes", subject)).items
    assert len(entries) == 1
    origin = entries[0].origin
    # what the TOOL supplied (a consumer-only WriteOrigin)
    assert origin.consumer == "state_apply"
    assert origin.run_id == "sess-42"
    assert origin.op_id == "op-1"
    assert origin.meta is None
    # what the PLATFORM stamped from the deposited context
    assert origin.door == "conversation"
    assert origin.actor == "user-7"
    assert origin.turn_id == "turn-9"
    assert entries[0].paths == [["n"]]


def test_state_apply_input_schema() -> None:
    schema = get_cached_typeadapter(builtin_states.state_apply).json_schema()
    props = schema["properties"]
    assert {"state", "ops", "subject", "op_id"} <= set(props)
    assert set(schema["required"]) == {"state", "ops"}


def test_state_tools_use_wrap_result_output_schema() -> None:
    # The tools return a record view / apply result as a JSON object, or null for a read
    # miss; the wrap-result schema surfaces every shape (including null) in result.data.
    assert builtin_states._RESULT_SCHEMA["x-fastmcp-wrap-result"] is True
    assert builtin_states._RESULT_SCHEMA["type"] == "object"
