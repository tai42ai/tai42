"""The states service: the feature gate, the declaration lifecycle, the attach-validator
seam (run BEFORE any write), subject validation, and the write-provenance chokepoint —
all against an in-memory fake store so the validate+apply logic is pinned without a live
Postgres (the store SQL is exercised in ``test_store_integration.py``)."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tai42_contract.states.errors import (
    DeclarationInUseError,
    NonAdditiveRedeclareError,
    StatesNotConfiguredError,
    SubjectRefusedError,
    TemplateValidationError,
)
from tai42_contract.states.models import (
    AttachBody,
    ConsumerRow,
    StateContext,
    StateDeclaration,
    StateTemplateDocument,
    SubjectCandidates,
    WriteOrigin,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService, state_context

from .fake_service_store import _STATE, FakeStatesStore, _subject


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def test_gate_refuses_501_when_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: False)
    svc = StatesService(store=FakeStatesStore())  # type: ignore[arg-type]
    with pytest.raises(StatesNotConfiguredError):
        await svc.list_declarations()
    with pytest.raises(StatesNotConfiguredError):
        await svc.read("alerts", _subject())


async def test_declaration_lifecycle(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    got = await svc.get_declaration("alerts")
    assert got is not None
    assert got.subject_kinds == ["thread"]
    # additive re-declare with records present is allowed
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "thread", "t1")] = {"n": 1}
    wider = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}, "m": {"type": "string"}}},
        subject_kinds=["thread", "session"],
        default_subject_kind="thread",
    )
    await svc.put_declaration(wider)


async def test_get_declaration_serves_the_composed_effective_schema(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    got = await svc.get_declaration("alerts")
    assert got is not None
    assert got.effective_schema == {"type": "object", "properties": {"n": {"type": "integer"}}}


async def test_put_declaration_refuses_a_client_supplied_effective_schema(svc: StatesService) -> None:
    forged = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
        effective_schema={"type": "object", "properties": {"forged": {"type": "string"}}},
    )
    with pytest.raises(ValueError, match="effective_schema is computed by the platform"):
        await svc.put_declaration(forged)


async def test_narrowing_redeclare_refused_with_records(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "thread", "t1")] = {"n": 1}
    narrower = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "string"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )
    with pytest.raises(NonAdditiveRedeclareError):
        await svc.put_declaration(narrower)


async def test_kind_removal_refused_with_records(svc: StatesService) -> None:
    two_kinds = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread", "person"],
        default_subject_kind="thread",
    )
    await svc.put_declaration(two_kinds)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "person", "p1")] = {"n": 1}
    with pytest.raises(DeclarationInUseError):
        await svc.put_declaration(_STATE)  # drops the 'person' kind still holding a record


async def test_delete_refused_while_a_hook_binds_the_state(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)

    async def hook_lister(_state: str):
        return [ConsumerRow(kind="hook", name="on-alert", detail="supplies kind thread")]

    svc.register_consumer_lister("hook", hook_lister)
    # A state referenced only by a hook (no record, no attach) is still in use — the
    # DeclarationInUseError guard reads the consumer union, so the hook blocks the delete.
    with pytest.raises(DeclarationInUseError, match="hook:on-alert"):
        await svc.delete_declaration("alerts")


async def test_delete_allowed_when_only_an_unavailable_family_is_listed(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)

    async def unavailable_lister(_state: str):
        return [ConsumerRow(kind="schedule", unavailable="no scheduling backend")]

    svc.register_consumer_lister("schedule", unavailable_lister)
    # A muted, cannot-list family is not a binder — it never blocks a delete.
    await svc.delete_declaration("alerts")
    assert await svc.get_declaration("alerts") is None


async def test_attach_validator_runs_before_write(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    template_doc = StateTemplateDocument.model_validate(
        {
            "kind": "state-template",
            "name": "tpl",
            "schema": {"type": "object", "properties": {"y": {"type": "integer"}}},
        }
    )
    await svc.put_template(template_doc, replace=False)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]

    async def refusing(doc, declarations, effective) -> None:
        raise TemplateValidationError("consumer says no")

    svc.register_attach_validator(refusing)
    before = store.upsert_attach_calls
    with pytest.raises(TemplateValidationError, match="consumer says no"):
        await svc.attach("alerts", "tpl", AttachBody(path=["sub"]))
    # the validator ran BEFORE the write — no attach row was stored
    assert store.upsert_attach_calls == before
    assert ("alerts", "tpl") not in store.attachments


async def test_attach_and_detach_recompose_effective(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    template_doc = StateTemplateDocument.model_validate(
        {
            "kind": "state-template",
            "name": "tpl",
            "schema": {"type": "object", "properties": {"y": {"type": "integer"}}},
        }
    )
    await svc.put_template(template_doc, replace=False)
    await svc.attach("alerts", "tpl", AttachBody(path=["sub"]))
    eff = await svc.effective_schema_for("alerts")
    assert "sub" in eff["properties"]
    await svc.detach("alerts", "tpl")
    eff2 = await svc.effective_schema_for("alerts")
    assert "sub" not in eff2["properties"]


async def test_validate_subject_undeclared_kind(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(SubjectRefusedError, match="not declared"):
        await svc.read("alerts", _subject(kind="bogus"))


async def test_validate_subject_person_branch(svc: StatesService, monkeypatch: pytest.MonkeyPatch) -> None:
    decl = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["person"],
        default_subject_kind="person",
    )
    await svc.put_declaration(decl)

    class _FakePersonStore:
        def __init__(self, settings) -> None:
            self._settings = settings

        async def get_by_id(self, person_id):
            if person_id == "known":
                return SimpleNamespace(person_id="known", target_kind="agent", target_name="a")
            return None

    import tai42_skeleton.conversations.persons as persons_mod
    import tai42_skeleton.conversations.settings as settings_mod

    monkeypatch.setattr(persons_mod, "ConversationPersonStore", _FakePersonStore)
    monkeypatch.setattr(settings_mod, "ConversationsSettings", lambda: object())

    # unknown person → refusal
    with pytest.raises(SubjectRefusedError, match="no person"):
        await svc.read("alerts", _subject(kind="person", key="ghost"))
    # target mismatch → refusal
    with pytest.raises(SubjectRefusedError, match="belongs to target"):
        await svc.read("alerts", _subject(kind="person", key="known", tn="other"))
    # known person, matching target → resolves (no record ⇒ None, not a refusal)
    assert await svc.read("alerts", _subject(kind="person", key="known")) is None


async def test_validate_subject_person_store_is_lazy_and_single(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``ConversationPersonStore`` is constructed ONLY on the ``person`` branch and
    lazily: a non-person subject never touches it — so no other kind is gated on the redis
    conversations backend — and a person subject constructs it exactly once."""
    import tai42_skeleton.conversations.persons as persons_mod
    import tai42_skeleton.conversations.settings as settings_mod

    ctor_calls = 0

    class _CountingPersonStore:
        def __init__(self, settings: object) -> None:
            nonlocal ctor_calls
            ctor_calls += 1

        async def get_by_id(self, person_id: str) -> object:
            return SimpleNamespace(person_id=person_id, target_kind="agent", target_name="a")

    monkeypatch.setattr(persons_mod, "ConversationPersonStore", _CountingPersonStore)

    # --- a non-person kind with the redis conversations backend ABSENT ---
    # constructing its settings would raise; a subject of another kind must resolve without
    # ever reaching the person branch, so neither the settings nor the store are touched.
    def _backend_absent() -> object:
        raise RuntimeError("the redis conversations backend is not configured")

    monkeypatch.setattr(settings_mod, "ConversationsSettings", _backend_absent)
    thread_decl = StateDeclaration(
        name="threads",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )
    await svc.put_declaration(thread_decl)
    assert await svc.read("threads", _subject(kind="thread")) is None
    assert ctor_calls == 0

    # --- the person kind with the backend present: the store is built lazily, exactly once ---
    monkeypatch.setattr(settings_mod, "ConversationsSettings", lambda: object())
    person_decl = StateDeclaration(
        name="people",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["person"],
        default_subject_kind="person",
    )
    await svc.put_declaration(person_decl)
    assert await svc.read("people", _subject(kind="person", key="known")) is None
    assert ctor_calls == 1


def _door_ctx(door, actor, turn_id, inbound_id) -> StateContext:
    return StateContext(
        door=door,
        candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "t1"}),
        actor=actor,
        turn_id=turn_id,
        inbound_id=inbound_id,
    )


# A completed origin per door across the four door contexts, plus the no-context ``api``
# fallback (``ctx is None`` → the bound request principal supplies the actor). Each carries
# the door's actor/turn_id/inbound_id that the write ledger and, under a traced attach, the
# ``_trace`` stamp are built from.
_PROVENANCE_DOORS = [
    pytest.param(
        _door_ctx("conversation", "user-7", "turn-9", "inb-3"),
        "conversation",
        "user-7",
        "turn-9",
        "inb-3",
        id="conversation",
    ),
    pytest.param(_door_ctx("hook", "hook-key", None, None), "hook", "hook-key", None, None, id="hook"),
    pytest.param(_door_ctx("schedule", None, None, None), "schedule", None, None, None, id="schedule"),
    pytest.param(None, "api", "principal-1", None, None, id="api-no-context"),
]


async def _attach_traced(svc: StatesService, store: FakeStatesStore) -> None:
    """A traced attach on ``alerts`` so an ``apply`` also exercises the ``_trace`` stamp
    at the fake store, populated directly (the attach lifecycle is pinned elsewhere)."""
    await svc.put_declaration(_STATE)
    store.templates["traced_m"] = {
        "name": "traced_m",
        "body": {
            "kind": "state-template",
            "name": "traced_m",
            "schema": {"type": "object"},
            "trace": {"enabled": True},
        },
        "shipped_hash": None,
        "updated_at": 1,
    }
    store.attachments[("alerts", "traced_m")] = {
        "state": "alerts",
        "template": "traced_m",
        "path": ["a"],
        "parameters": {},
        "declarations": {},
        "updated_at": 1,
    }


@pytest.mark.parametrize(("ctx", "exp_door", "exp_actor", "exp_turn", "exp_inbound"), _PROVENANCE_DOORS)
async def test_write_provenance_completed_per_door(
    svc: StatesService,
    monkeypatch: pytest.MonkeyPatch,
    ctx: StateContext | None,
    exp_door: str,
    exp_actor: str | None,
    exp_turn: str | None,
    exp_inbound: str | None,
) -> None:
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    await _attach_traced(svc, store)
    if ctx is None:
        # no ambient context ⇒ door ``api`` + the bound request principal as actor
        import tai42_skeleton.access_control.user as user_mod

        monkeypatch.setattr(user_mod, "request_identity", lambda: ("principal-1", None))
    ops = [{"op": "set", "path": ["a", "obj"], "value": {"k": 1}}]
    cm = state_context(ctx) if ctx is not None else nullcontext()
    with cm:
        await svc.apply(
            "alerts",
            _subject(),
            ops,
            op_id=None,
            origin=WriteOrigin(consumer="consumer-x", meta={"node": "node1"}, run_id="run2"),
        )
    # the completed origin the ledger records: the door's context stamps door/actor/turn_id,
    # the consumer's own fields (consumer/meta/run_id) survive every door.
    origin = store.applied_origins[-1]
    assert origin.door == exp_door
    assert origin.actor == exp_actor
    assert origin.turn_id == exp_turn
    assert origin.inbound_id == exp_inbound
    assert (origin.consumer, origin.meta, origin.run_id) == ("consumer-x", {"node": "node1"}, "run2")
    # a traced attach stamps ``_trace`` end to end carrying the SAME door's fields
    stamp = ops[0]["value"]["_trace"]
    assert stamp["meta"] == {"node": "node1"}
    assert stamp["run"] == "run2"
    assert stamp["turn"] == exp_turn
    assert stamp["inbound"] == exp_inbound
    assert "at" in stamp


def test_consumer_supplied_door_refused_at_model() -> None:
    # ``door``/``actor``/``turn_id`` are absent from WriteOrigin (extra='forbid'), so a
    # consumer cannot forge them — the model itself refuses.
    with pytest.raises(ValidationError):
        WriteOrigin(consumer="x", door="conversation")  # type: ignore[call-arg]
