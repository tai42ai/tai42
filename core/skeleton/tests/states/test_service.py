"""The states service: the feature gate, the declaration lifecycle, the mount-validator
seam (run BEFORE any write), subject validation, and the write-provenance chokepoint —
all against an in-memory fake store so the validate+apply logic is pinned without a live
Postgres (the store SQL is exercised in ``test_store_integration.py``)."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.errors import (
    DeclarationInUseError,
    ModuleValidationError,
    NonAdditiveRedeclareError,
    StateNotFoundError,
    StatesNotConfiguredError,
    SubjectRefusedError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    ConsumerRow,
    MountBody,
    StateContext,
    StateDeclaration,
    StateModuleDocument,
    StateSubject,
    SubjectCandidates,
    WriteOrigin,
    WritesPage,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService, state_context
from tai42_skeleton.states.store import _iso_now, _traced_paths, stamp_trace


class FakeStatesStore:
    """An in-memory stand-in for :class:`PostgresStatesStore` covering the methods the
    service drives — enough to pin the service's validate+apply logic."""

    def __init__(self) -> None:
        self.declarations: dict[str, dict[str, Any]] = {}
        self.modules: dict[str, dict[str, Any]] = {}
        self.mounts: dict[tuple[str, str], dict[str, Any]] = {}
        self.records: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.write_rows: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        self.applied_origins: list[Any] = []
        self.upsert_mount_calls = 0
        self.update_decl_calls = 0
        self.upsert_module_calls = 0

    # declarations
    async def get_declaration(self, name):
        return self.declarations.get(name)

    async def list_declarations(self):
        return list(self.declarations.values())

    async def upsert_declaration_guarded(
        self,
        name,
        description,
        schema,
        subject_kinds,
        default_subject_kind,
        retention_days,
        *,
        effective_schema,
        decide,
    ):
        existing = self.declarations.get(name)
        per_kind: dict[str, int] = {}
        for state, _tk, _tn, sk, _key in self.records:
            if state == name:
                per_kind[sk] = per_kind.get(sk, 0) + 1
        decide(existing, per_kind)
        self.declarations[name] = {
            "name": name,
            "description": description,
            "schema": schema,
            "effective_schema": effective_schema,
            "subject_kinds": list(subject_kinds),
            "default_subject_kind": default_subject_kind,
            "retention_days": retention_days,
            "updated_at": 1,
        }

    async def delete_declaration(self, name):
        return self.declarations.pop(name, None) is not None

    async def field_stats(self, state):
        per_kind: dict[str, int] = {}
        for s, _tk, _tn, sk, _key in self.records:
            if s == state:
                per_kind[sk] = per_kind.get(sk, 0) + 1
        return sum(per_kind.values()), {}, per_kind

    # modules
    async def get_module(self, name):
        return self.modules.get(name)

    async def list_modules(self):
        return list(self.modules.values())

    async def mounted_module_counts(self):
        counts: dict[str, int] = {}
        for _s, module in self.mounts:
            counts[module] = counts.get(module, 0) + 1
        return counts

    async def writes(self, state, subject, *, limit, cursor):
        rows = list(
            self.write_rows.get((state, subject.target_kind, subject.target_name, subject.kind, subject.key), [])
        )
        start = 0 if cursor is None else next((i for i, r in enumerate(rows) if r["id"] < int(cursor)), len(rows))
        return rows[start : start + limit]

    async def upsert_module(self, name, body, shipped_hash):
        self.upsert_module_calls += 1
        self.modules[name] = {"name": name, "body": body, "shipped_hash": shipped_hash, "updated_at": 1}

    async def delete_module(self, name):
        return self.modules.pop(name, None) is not None

    # mounts
    async def get_mount(self, state, module):
        return self.mounts.get((state, module))

    async def list_mounts_for_state(self, state):
        return [v for (s, _m), v in self.mounts.items() if s == state]

    async def list_mounts_of_module(self, module):
        return [v for (_s, m), v in self.mounts.items() if m == module]

    async def list_all_mounts(self):
        return list(self.mounts.values())

    async def upsert_mount(self, state, module, path, parameters, declarations, *, effective_schema):
        self.upsert_mount_calls += 1
        self.mounts[(state, module)] = {
            "state": state,
            "module": module,
            "path": path,
            "parameters": parameters,
            "declarations": declarations,
            "updated_at": 1,
        }
        self.declarations[state]["effective_schema"] = effective_schema

    async def update_mount_declarations(self, state, module, declarations, *, effective_schema):
        self.update_decl_calls += 1
        self.mounts[(state, module)]["declarations"] = declarations
        self.declarations[state]["effective_schema"] = effective_schema
        return True

    async def update_mount_parameters(self, state, module, parameters, *, effective_schema):
        self.mounts[(state, module)]["parameters"] = parameters
        self.declarations[state]["effective_schema"] = effective_schema
        return True

    async def delete_mount(self, state, module, *, effective_schema):
        self.mounts.pop((state, module), None)
        self.declarations[state]["effective_schema"] = effective_schema
        return True

    # records
    async def read_record_view(self, state, subject):
        row = self.records.get((state, subject.target_kind, subject.target_name, subject.kind, subject.key))
        if row is None:
            return None
        return {"data": row, "seq": 1.0, "canonical_subject": subject, "folded_from": []}

    async def apply_ops(self, state, subject, ops, *, op_id, origin, validate_doc, retention_days):
        self.applied_origins.append(origin)
        # Mirror the store's D-3 chokepoint so the service-level provenance test is end to
        # end: compose the state's traced paths from its mounts + modules and stamp
        # ``_trace`` from the COMPLETED origin (the stamping mechanics themselves are pinned
        # in test_store.py). A state with no traced mount leaves the ops untouched.
        mount_rows = [
            {"module": m["module"], "path": m["path"], "body": self.modules[m["module"]]["body"]}
            for (s, _module), m in self.mounts.items()
            if s == state and m["module"] in self.modules
        ]
        traced = _traced_paths(mount_rows)
        if traced:
            stamp = {
                "meta": origin.meta,
                "run": origin.run_id,
                "turn": origin.turn_id,
                "inbound": origin.inbound_id,
                "at": _iso_now(),
            }
            stamp_trace(ops, traced, stamp)
        doc: dict[str, Any] = {}
        for op in ops:
            if op["op"] == "set":
                doc[op["path"][0]] = op["value"]
        self.records[(state, subject.target_kind, subject.target_name, subject.kind, subject.key)] = doc
        return (True, doc, 1.0, [])

    async def import_records(self, state, rows, *, origin, validate_doc):
        decl = self.declarations.get(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        self.applied_origins.append(origin)
        for row in rows:
            validate_doc(decl["effective_schema"], row["data"])
            key = (state, row["target_kind"], row["target_name"], row["subject_kind"], row["subject_key"])
            self.records[key] = row["data"]


_STATE = StateDeclaration(
    name="alerts",
    schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    subject_kinds=["thread"],
    default_subject_kind="thread",
)


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


def _subject(kind="thread", key="t1", tk: ConversationTargetKind = "agent", tn="a") -> StateSubject:
    return StateSubject(target_kind=tk, target_name=tn, kind=kind, key=key)


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
    # A state referenced only by a hook (no record, no mount) is still in use — the
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


async def test_mount_validator_runs_before_write(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    module_doc = StateModuleDocument.model_validate(
        {"kind": "state-module", "name": "mod", "schema": {"type": "object", "properties": {"y": {"type": "integer"}}}}
    )
    await svc.put_module(module_doc, replace=False)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]

    async def refusing(doc, declarations, effective) -> None:
        raise ModuleValidationError("consumer says no")

    svc.register_mount_validator(refusing)
    before = store.upsert_mount_calls
    with pytest.raises(ModuleValidationError, match="consumer says no"):
        await svc.mount("alerts", "mod", MountBody(path=["sub"]))
    # the validator ran BEFORE the write — no mount row was stored
    assert store.upsert_mount_calls == before
    assert ("alerts", "mod") not in store.mounts


async def test_mount_and_unmount_recompose_effective(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    module_doc = StateModuleDocument.model_validate(
        {"kind": "state-module", "name": "mod", "schema": {"type": "object", "properties": {"y": {"type": "integer"}}}}
    )
    await svc.put_module(module_doc, replace=False)
    await svc.mount("alerts", "mod", MountBody(path=["sub"]))
    eff = await svc.effective_schema_for("alerts")
    assert "sub" in eff["properties"]
    await svc.unmount("alerts", "mod")
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


# The four door contexts the plan names (§4.6): a completed origin per door, plus the
# no-context ``api`` fallback (``ctx is None`` → the bound request principal supplies the
# actor). Each carries the door's actor/turn_id/inbound_id that the write ledger and, under
# a traced mount, the ``_trace`` stamp are built from.
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


async def _mount_traced(svc: StatesService, store: FakeStatesStore) -> None:
    """A traced mount on ``alerts`` so an ``apply`` also exercises the D-3 ``_trace`` stamp
    at the fake store, populated directly (the mount lifecycle is pinned elsewhere)."""
    await svc.put_declaration(_STATE)
    store.modules["traced_m"] = {
        "name": "traced_m",
        "body": {"kind": "state-module", "name": "traced_m", "schema": {"type": "object"}, "trace": {"enabled": True}},
        "shipped_hash": None,
        "updated_at": 1,
    }
    store.mounts[("alerts", "traced_m")] = {
        "state": "alerts",
        "module": "traced_m",
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
    await _mount_traced(svc, store)
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
    # a traced mount stamps ``_trace`` end to end carrying the SAME door's fields
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


# --------------------------------------------------------------------------- #
# served regimes (PLAN_5 item 2)                                              #
# --------------------------------------------------------------------------- #
_REGIME_MODULE = {
    "kind": "state-module",
    "name": "tagmod",
    "schema": {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
    "regimes": [{"path": ["tags"], "regime": "composing"}],
}


async def test_get_declaration_serves_regimes_for_a_mount_and_empty_for_none(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    # no mounts ⇒ the platform serves an empty regime list, never None-on-the-wire noise
    unmounted = await svc.get_declaration("alerts")
    assert unmounted is not None
    assert unmounted.regimes == []
    # mount a module declaring a composing regime; the served regime is ABSOLUTE (mount
    # path prefixed onto the module's regime path) and matches served_declaration
    await svc.put_module(StateModuleDocument.model_validate(_REGIME_MODULE), replace=False)
    await svc.mount("alerts", "tagmod", MountBody(path=["sub"]))
    mounted = await svc.get_declaration("alerts")
    assert mounted is not None
    assert mounted.regimes == [{"path": ["sub", "tags"], "regime": "composing"}]
    served = await svc.served_declaration("alerts")
    assert mounted.regimes == served["regimes"]


async def test_list_declarations_serves_composed_regimes(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_module(StateModuleDocument.model_validate(_REGIME_MODULE), replace=False)
    await svc.mount("alerts", "tagmod", MountBody(path=["sub"]))
    decls = await svc.list_declarations()
    assert [d.regimes for d in decls] == [[{"path": ["sub", "tags"], "regime": "composing"}]]


async def test_put_declaration_refuses_a_client_supplied_regimes(svc: StatesService) -> None:
    forged = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
        regimes=[{"path": ["forged"], "regime": "single"}],
    )
    with pytest.raises(ValueError, match="regimes are computed by the platform"):
        await svc.put_declaration(forged)


async def test_declaration_serves_updated_at_on_get_and_list_and_refuses_it_on_put(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    got = await svc.get_declaration("alerts")
    assert got is not None
    assert got.updated_at is not None
    listed = await svc.list_declarations()
    assert [d.updated_at for d in listed] == [got.updated_at]
    forged = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
        updated_at="2026-09-06T00:00:00Z",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="updated_at is set by the platform"):
        await svc.put_declaration(forged)


async def test_served_declaration_serves_updated_at_matching_the_list_iso_format(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    served = await svc.served_declaration("alerts")
    listed = (await svc.list_declarations())[0]
    # The single read (GET /api/states/{name}) serves ``updated_at`` in the SAME ISO string
    # the list read serves — one format across both doors, never two.
    assert isinstance(served["updated_at"], str)
    assert served["updated_at"] == listed.model_dump(mode="json")["updated_at"]


async def test_served_declaration_serves_retention_days_matching_the_list_read(svc: StatesService) -> None:
    # A configured retention must ride the single read (GET /api/states/{name}) so an edit
    # form round-trips it; omitting it would let a full-declaration PUT clear the setting.
    await svc.put_declaration(
        StateDeclaration(
            name="alerts",
            schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            subject_kinds=["thread"],
            default_subject_kind="thread",
            retention_days=30,
        )
    )
    served = await svc.served_declaration("alerts")
    listed = (await svc.list_declarations())[0]
    assert served["retention_days"] == 30
    assert served["retention_days"] == listed.model_dump(mode="json")["retention_days"]


async def test_writes_returns_a_keyset_page_with_next_cursor(svc: StatesService) -> None:
    from datetime import UTC, datetime

    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    subject = _subject()
    key = ("alerts", subject.target_kind, subject.target_name, subject.kind, subject.key)
    store.write_rows[key] = [
        {
            "id": rid,
            "seq": float(rid),
            "at": datetime(2026, 1, 1, tzinfo=UTC),
            "door": "api",
            "actor": None,
            "consumer": None,
            "meta": None,
            "run_id": None,
            "op_id": None,
            "turn_id": None,
            "paths": [["n"]],
        }
        for rid in (3, 2, 1)
    ]
    first = await svc.writes("alerts", subject, limit=2)
    assert isinstance(first, WritesPage)
    assert [e.seq for e in first.items] == [3.0, 2.0]
    # A full page hands back the last row's id as the cursor for the next call.
    assert first.next_cursor == "2"
    second = await svc.writes("alerts", subject, limit=2, cursor=first.next_cursor)
    assert [e.seq for e in second.items] == [1.0]
    # The last page is exhausted, so it carries no cursor.
    assert second.next_cursor is None


async def test_writes_refuses_a_malformed_cursor_with_a_value_error(svc: StatesService) -> None:
    # A client-supplied opaque cursor that is not a row id is a 422 (ValueValidationError →
    # ValidationRejected at the door), never a 500 from ``int()`` deep in the store.
    with pytest.raises(ValueValidationError, match="cursor"):
        await svc.writes("alerts", _subject(), limit=2, cursor="not-a-row-id")


async def test_list_modules_catalog_adds_mounted_on_and_shipped_default(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_module(StateModuleDocument.model_validate(_REGIME_MODULE), replace=False)
    await svc.mount("alerts", "tagmod", MountBody(path=["sub"]))
    # A second, operator-uploaded module (no shipped_hash) that is mounted nowhere.
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.modules["loose"] = {
        "name": "loose",
        "body": {"kind": "state-module", "name": "loose", "schema": {"type": "object"}},
        "shipped_hash": None,
        "updated_at": 1,
    }
    # Mark the mounted module as an unedited shipped default.
    store.modules["tagmod"]["shipped_hash"] = "abc123"
    catalog = {row["name"]: row for row in await svc.list_modules_catalog()}
    assert catalog["tagmod"]["mounted_on"] == 1
    assert catalog["tagmod"]["shipped_default"] is True
    assert catalog["loose"]["mounted_on"] == 0
    assert catalog["loose"]["shipped_default"] is False


# --------------------------------------------------------------------------- #
# import_records validates every subject before any write (PLAN_5 item 3)      #
# --------------------------------------------------------------------------- #
_PERSON_STATE = StateDeclaration(
    name="alerts",
    schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    subject_kinds=["person"],
    default_subject_kind="person",
)


def _patch_person_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``known`` resolves, to target ``agent/a`` — every other id is unknown."""

    class _FakePersonStore:
        def __init__(self, settings: object) -> None:
            self._settings = settings

        async def get_by_id(self, person_id: str) -> object:
            if person_id == "known":
                return SimpleNamespace(person_id="known", target_kind="agent", target_name="a")
            return None

    import tai42_skeleton.conversations.persons as persons_mod
    import tai42_skeleton.conversations.settings as settings_mod

    monkeypatch.setattr(persons_mod, "ConversationPersonStore", _FakePersonStore)
    monkeypatch.setattr(settings_mod, "ConversationsSettings", lambda: object())


def _person_row(key: str, n: int) -> dict[str, Any]:
    return {"target_kind": "agent", "target_name": "a", "subject_kind": "person", "subject_key": key, "data": {"n": n}}


async def test_import_records_refuses_a_bad_person_row_and_writes_nothing(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await svc.put_declaration(_PERSON_STATE)
    _patch_person_store(monkeypatch)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    rows = [_person_row("known", 1), _person_row("ghost", 2)]
    # the offending row's index AND its subject ride the refusal, and nothing is written
    with pytest.raises(SubjectRefusedError, match="import row 1"):
        await svc.import_records("alerts", rows, origin=WriteOrigin(consumer="flow_states_transfer"))
    assert store.records == {}


async def test_import_records_lands_a_clean_batch(svc: StatesService, monkeypatch: pytest.MonkeyPatch) -> None:
    await svc.put_declaration(_PERSON_STATE)
    _patch_person_store(monkeypatch)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    ctx = StateContext(
        door="transfer",
        candidates=SubjectCandidates(target_kind="agent", target_name="a"),
        actor="operator-1",
    )
    with state_context(ctx):
        await svc.import_records(
            "alerts", [_person_row("known", 7)], origin=WriteOrigin(consumer="flow_states_transfer")
        )
    assert store.records[("alerts", "agent", "a", "person", "known")] == {"n": 7}
    assert store.applied_origins[-1].door == "transfer"
