"""The states service's record, module and mount doors, plus the pure schema/parameter
validators — driven against the in-memory ``FakeStatesStore`` (no live database). The
declaration lifecycle, gate, subject validation and write-provenance chokepoint are pinned
in ``test_service.py``; this file covers every remaining service branch that the real-store
integration test otherwise exercises.
"""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import (
    InvalidPathError,
    ModuleExistsError,
    ModuleInUseError,
    ModuleValidationError,
    MountConflictError,
    SchemaValidationError,
    StateNotFoundError,
    SubjectFoldError,
    SubjectRefusedError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    MountBody,
    StateModuleDocument,
    WriteOrigin,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.modules import validate_module
from tai42_skeleton.states.service import (
    StatesService,
    _page_limit,
    _validate_document,
    _validate_schema,
)

from .test_service import _STATE, FakeStatesStore, _subject

_ORIGIN = WriteOrigin(consumer="c")


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


def _module_doc(name="mod", **over):
    body = {
        "kind": "state-module",
        "name": name,
        "schema": {"type": "object", "properties": {"y": {"type": "integer"}}},
    }
    body.update(over)
    return StateModuleDocument.model_validate(body)


# --------------------------------------------------------------------------- #
# pure schema validators                                                        #
# --------------------------------------------------------------------------- #
def test_validate_schema_shape_refusals() -> None:
    with pytest.raises(SchemaValidationError, match="must be a JSON object"):
        _validate_schema("nope")
    with pytest.raises(SchemaValidationError, match='"type": "object"'):
        _validate_schema({"type": "string"})
    with pytest.raises(SchemaValidationError, match="at least one property"):
        _validate_schema({"type": "object", "properties": {}})
    with pytest.raises(SchemaValidationError, match="not a valid JSON Schema"):
        _validate_schema({"type": "object", "properties": {"n": {"type": 123}}})


def test_validate_schema_accepts_resolvable_local_refs() -> None:
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/x"}, "b": {"$ref": "#"}, "c": {"$ref": "#/allOf/0"}},
            "allOf": [{"title": "t"}],
            "$defs": {"x": {"type": "integer"}},
        }
    )
    # a #anchor that resolves (nested inside a list, exercising the list-walk)
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#named"}},
            "allOf": [{"$anchor": "named", "type": "object"}],
        }
    )


def test_validate_schema_percent_decodes_refs() -> None:
    # RFC 6901 §6: a URI-fragment pointer is percent-decoded WHOLE, then split on "/", then
    # ~1/~0-unescaped — matching jsonschema's resolver, so this syntax pre-check accepts
    # exactly what _validate_document later resolves. "%20" decodes to a space in the key.
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/a%20b"}},
            "$defs": {"a b": {"type": "integer"}},
        }
    )
    # A key that literally contains "/" is named with ~1 (RFC 6901), never %2F: %2F decodes
    # to a separator before the split, so only #/$defs/a~1b reaches the key "a/b".
    _validate_schema(
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/a~1b"}},
            "$defs": {"a/b": {"type": "integer"}},
        }
    )
    # %2F decodes to a separator before the split, so it can never name a key holding "/".
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/a%2Fb"}},
                "$defs": {"a/b": {"type": "integer"}},
            }
        )
    # A percent-encoded ref that names no key is still refused loudly.
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema(
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/x%20y"}},
                "$defs": {"a b": {"type": "integer"}},
            }
        )


def test_validate_schema_ref_refusals() -> None:
    with pytest.raises(SchemaValidationError, match="remote"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}})
    with pytest.raises(SchemaValidationError, match="does not resolve"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})
    with pytest.raises(SchemaValidationError, match="no \\$anchor"):
        _validate_schema({"type": "object", "properties": {"a": {"$ref": "#absent"}}})
    with pytest.raises(SchemaValidationError, match="dynamicRef"):
        _validate_schema({"type": "object", "properties": {"x": {"type": "string"}}, "allOf": [{"$dynamicRef": "#m"}]})


def test_validate_document_reports_unresolvable_ref() -> None:
    # A schema whose $ref cannot be resolved at validation time surfaces as a loud value
    # error, never an opaque referencing exception.
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    with pytest.raises(ValueValidationError):
        _validate_document(schema, {"a": 1})


def test_page_limit_clamps_and_refuses() -> None:
    assert _page_limit(None) == 200
    assert _page_limit(10) == 10
    assert _page_limit(10_000) == 500  # clamped to the hard cap
    with pytest.raises(ValueValidationError, match="positive integer"):
        _page_limit(0)
    with pytest.raises(ValueValidationError, match="positive integer"):
        _page_limit(True)


# --------------------------------------------------------------------------- #
# records: replace / merge / apply / erase / fold                               #
# --------------------------------------------------------------------------- #
async def test_replace_writes_and_reads_back(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    view = await svc.replace("alerts", _subject(), {"n": 5}, origin=_ORIGIN)
    assert view.data == {"n": 5}


async def test_replace_refuses_non_object(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(ValueValidationError, match="must be a JSON object"):
        await svc.replace("alerts", _subject(), ["not", "an", "object"], origin=_ORIGIN)  # type: ignore[arg-type]


async def test_merge_applies_top_level_patch(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    view = await svc.merge("alerts", _subject(), {"n": 7}, origin=_ORIGIN)
    assert view.data == {"n": 7}


async def test_merge_refuses_non_object(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(ValueValidationError, match="merge patch must be a JSON object"):
        await svc.merge("alerts", _subject(), [1, 2], origin=_ORIGIN)  # type: ignore[arg-type]


async def test_merge_empty_patch_no_record_returns_empty_view(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    view = await svc.merge("alerts", _subject(), {}, origin=_ORIGIN)
    assert view.data == {}
    assert view.seq == 0.0


async def test_apply_refuses_non_list_ops(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(InvalidPathError, match="ops must be a list"):
        await svc.apply("alerts", _subject(), "nope", op_id=None, origin=_ORIGIN)  # type: ignore[arg-type]


async def test_apply_empty_ops_is_a_noop_result(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    result = await svc.apply("alerts", _subject(), [], op_id=None, origin=_ORIGIN)
    assert result.applied is False
    assert result.data is None


async def test_erase_removes_the_record(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(), {"n": 1}, origin=_ORIGIN)
    await svc.erase("alerts", _subject(), origin=_ORIGIN)
    assert await svc.read("alerts", _subject()) is None


async def test_fold_delegates_to_the_store(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    report = await svc.fold("alerts", _subject(key="old"), _subject(key="new"), "switch", origin=_ORIGIN)
    assert report["mode"] == "switch"
    assert report["into"]["key"] == "new"


async def test_fold_refuses_an_unknown_mode(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(SubjectFoldError, match="unknown fold mode"):
        await svc.fold("alerts", _subject(key="old"), _subject(key="new"), "bogus", origin=_ORIGIN)


# --------------------------------------------------------------------------- #
# listing / search / prune                                                      #
# --------------------------------------------------------------------------- #
async def test_list_subjects_pages_with_a_next_cursor(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "thread", "t1")] = {"n": 1}
    store.records[("alerts", "agent", "a", "thread", "t2")] = {"n": 2}
    page = await svc.list_subjects("alerts", limit=1)
    assert len(page["subjects"]) == 1
    assert page["next_cursor"] is not None  # a full page hands back a cursor


async def test_list_subjects_undeclared_raises(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError):
        await svc.list_subjects("nope")


async def test_search_matches_containment(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "thread", "t1")] = {"n": 1}
    store.records[("alerts", "agent", "a", "thread", "t2")] = {"n": 2}
    page = await svc.search("alerts", {"n": 1})
    assert [m["subject"]["key"] for m in page["matches"]] == ["t1"]
    assert page["next_cursor"] is None


async def test_search_refuses_empty_filters(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(ValueValidationError, match="non-empty filters"):
        await svc.search("alerts", {})


async def test_search_undeclared_raises(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError):
        await svc.search("nope", {"n": 1})


async def test_prune_expired_reports_counts(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "thread", "t1")] = {"n": 1}
    counts = await svc.prune_expired()
    assert counts == {"alerts": 2}


async def test_prune_expired_refuses_a_misconfigured_default(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_mod, "store_settings_default_retention", lambda: 0)
    with pytest.raises(ValueValidationError, match="DEFAULT_RETENTION_DAYS"):
        await svc.prune_expired()


# --------------------------------------------------------------------------- #
# backup restore doors                                                          #
# --------------------------------------------------------------------------- #
async def test_restore_records_refuses_a_malformed_subject(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    rows = [{"target_kind": "agent", "target_name": "a", "subject_kind": "thread", "subject_key": "", "data": {"n": 1}}]
    with pytest.raises(SubjectRefusedError, match="malformed subject"):
        await svc.restore_records("alerts", rows, origin=_ORIGIN)


async def test_restore_records_refuses_an_undeclared_kind(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    rows = [
        {"target_kind": "agent", "target_name": "a", "subject_kind": "ghost", "subject_key": "k1", "data": {"n": 1}}
    ]
    with pytest.raises(SubjectRefusedError, match="restore row 0"):
        await svc.restore_records("alerts", rows, origin=_ORIGIN)


async def test_restore_aliases_delegates(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.restore_aliases("alerts", [{"alias_kind": "thread", "alias_key": "o"}], origin=_ORIGIN)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert store.restored_aliases == [{"alias_kind": "thread", "alias_key": "o"}]


# --------------------------------------------------------------------------- #
# modules                                                                       #
# --------------------------------------------------------------------------- #
async def test_list_and_get_module(svc: StatesService) -> None:
    await svc.put_module(_module_doc("m1"), replace=False)
    listed = await svc.list_modules()
    assert [m.name for m in listed] == ["m1"]
    got = await svc.get_module("m1")
    assert got is not None
    assert got.name == "m1"
    assert await svc.get_module("absent") is None


async def test_put_module_without_replace_refuses_existing(svc: StatesService) -> None:
    await svc.put_module(_module_doc("m"), replace=False)
    with pytest.raises(ModuleExistsError, match="already exists"):
        await svc.put_module(_module_doc("m"), replace=False)


async def test_put_module_replace_revalidates_live_mounts(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_module(_module_doc("m"), replace=False)
    await svc.mount("alerts", "m", MountBody(path=["sub"]))
    # a replace with a still-compatible body backfills the mount's parameters and succeeds
    await svc.put_module(
        _module_doc("m", schema={"type": "object", "properties": {"y": {"type": "integer"}, "z": {"type": "string"}}}),
        replace=True,
    )
    got = await svc.get_module("m")
    assert got is not None
    assert "z" in got.schema_["properties"]


async def test_put_module_replace_refused_when_a_validator_now_rejects(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_module(_module_doc("m"), replace=False)
    await svc.mount("alerts", "m", MountBody(path=["sub"]))

    async def refusing(doc, declarations, effective) -> None:
        raise ModuleValidationError("no longer valid on this mount")

    svc.register_mount_validator(refusing)
    with pytest.raises(ModuleInUseError, match="no longer validates"):
        await svc.put_module(_module_doc("m"), replace=True)


async def test_delete_module_paths(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no module"):
        await svc.delete_module("absent")
    await svc.put_declaration(_STATE)
    await svc.put_module(_module_doc("m"), replace=False)
    await svc.mount("alerts", "m", MountBody(path=["sub"]))
    with pytest.raises(ModuleInUseError, match="mounted on state"):
        await svc.delete_module("m")
    await svc.unmount("alerts", "m")
    await svc.delete_module("m")
    assert await svc.get_module("m") is None


# --------------------------------------------------------------------------- #
# mounts                                                                        #
# --------------------------------------------------------------------------- #
async def test_list_mounts_every_form(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_module(_module_doc("m"), replace=False)
    await svc.mount("alerts", "m", MountBody(path=["sub"]))
    assert len(await svc.list_mounts("alerts", module="m")) == 1
    assert await svc.list_mounts("alerts", module="absent") == []
    assert len(await svc.list_mounts("alerts")) == 1
    assert len(await svc.list_mounts(module="m")) == 1
    assert len(await svc.list_mounts()) == 1


async def test_mount_refuses_a_duplicate(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_module(_module_doc("m"), replace=False)
    await svc.mount("alerts", "m", MountBody(path=["sub"]))
    with pytest.raises(MountConflictError, match="already mounted"):
        await svc.mount("alerts", "m", MountBody(path=["other"]))


async def test_mount_missing_module_raises(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(StateNotFoundError, match="no module"):
        await svc.mount("alerts", "absent", MountBody(path=["sub"]))


async def test_effective_schema_for_undeclared_raises(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no state declared"):
        await svc.effective_schema_for("absent")


def test_register_consumer_lister_refuses_duplicate_kind(svc: StatesService) -> None:
    async def lister(_state: str):
        return []

    svc.register_consumer_lister("hook", lister)
    with pytest.raises(ValueError, match="already registered"):
        svc.register_consumer_lister("hook", lister)


async def test_update_mount_declarations_paths(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    decl_module = _module_doc(
        "m",
        declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    )
    await svc.put_module(decl_module, replace=False)
    await svc.mount("alerts", "m", MountBody(path=["sub"], declarations={"n": 1}))
    await svc.update_mount_declarations("alerts", "m", {"n": 2})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert store.mounts[("alerts", "m")]["declarations"] == {"n": 2}
    with pytest.raises(StateNotFoundError, match="not mounted"):
        await svc.update_mount_declarations("alerts", "absent", {})


async def test_unmount_missing_raises(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(StateNotFoundError, match="not mounted"):
        await svc.unmount("alerts", "absent")


def test_regime_for_path(svc: StatesService) -> None:
    module = validate_module(
        {
            "kind": "state-module",
            "name": "m",
            "schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}},
            "regimes": [{"path": ["items"], "regime": "composing"}],
        }
    )
    assert svc.regime_for_path(module, ["items"]) == "composing"


# --------------------------------------------------------------------------- #
# seeds + module cache + misc helpers                                           #
# --------------------------------------------------------------------------- #
async def test_register_and_apply_module_seeds(svc: StatesService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    svc.register_module_seed(_module_doc("seeded"))
    await svc.apply_module_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "seeded" in store.modules


async def test_apply_module_seeds_is_a_noop_when_feature_off(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.register_module_seed(_module_doc("seeded"))
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: False)
    await svc.apply_module_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "seeded" not in store.modules


async def test_module_cache_serves_hit_and_evicts(svc: StatesService) -> None:
    svc._MODULE_CACHE_MAX = 1  # type: ignore[misc]
    await svc.put_module(_module_doc("m1"), replace=False)
    await svc.put_module(_module_doc("m2"), replace=False)
    await svc.get_module("m1")  # populates the cache
    await svc.get_module("m1")  # a cache hit (move-to-end)
    await svc.get_module("m2")  # overflows the bounded cache → eviction
    assert len(svc._module_cache) == 1


async def test_delete_declaration_not_found(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no state declared"):
        await svc.delete_declaration("absent")


async def test_stats_projects_fields_and_consumers(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.records[("alerts", "agent", "a", "thread", "t1")] = {"n": 1}
    stats = await svc.stats("alerts")
    assert stats["records"] == 1
    assert set(stats["per_field"]) == {"n"}
    assert stats["consumers"] == 0


async def test_stats_undeclared_raises(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError):
        await svc.stats("absent")


# --------------------------------------------------------------------------- #
# mount-value validation (unknown/invalid/missing params, declarations, check) #
# --------------------------------------------------------------------------- #
async def test_validate_mount_values_parameter_branches(svc: StatesService) -> None:
    param_module = validate_module(
        {
            "kind": "state-module",
            "name": "m",
            "schema": {"type": "object", "properties": {"cap": {"$parameter": "cap"}}},
            "parameters": {"cap": {"schema": {"type": "integer"}}},
        }
    )
    with pytest.raises(ModuleValidationError, match="unknown parameter"):
        await svc._validate_mount_values(param_module, {"nope": 1}, {})
    with pytest.raises(ModuleValidationError, match="is invalid"):
        await svc._validate_mount_values(param_module, {"cap": "not-an-int"}, {})
    with pytest.raises(ModuleValidationError, match="must supply parameter"):
        await svc._validate_mount_values(param_module, {}, {})


async def test_validate_mount_values_declaration_branches(svc: StatesService) -> None:
    plain = validate_module(
        {"kind": "state-module", "name": "m", "schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
    )
    with pytest.raises(ModuleValidationError, match="declares no declarations section"):
        await svc._validate_mount_values(plain, {}, {"x": 1})

    checked = validate_module(
        {
            "kind": "state-module",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}, "check": ".n > 0"},
        }
    )
    with pytest.raises(ModuleValidationError, match="invalid under module"):
        await svc._validate_mount_values(checked, {}, {"n": "bad"})
    with pytest.raises(ModuleValidationError, match="rejected by module"):
        await svc._validate_mount_values(checked, {}, {"n": -1})
    # a passing declaration set clears every gate (the happy path through the check)
    await svc._validate_mount_values(checked, {}, {"n": 3})


async def test_validate_mount_values_check_string_message_and_eval_error(svc: StatesService) -> None:
    stringy = validate_module(
        {
            "kind": "state-module",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "check": 'if .n > 0 then true else "n must be positive" end',
            },
        }
    )
    with pytest.raises(ModuleValidationError, match="n must be positive"):
        await svc._validate_mount_values(stringy, {}, {"n": -1})

    erroring = validate_module(
        {
            "kind": "state-module",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "check": '.n | error("boom")',
            },
        }
    )
    with pytest.raises(ModuleValidationError, match="failed to evaluate"):
        await svc._validate_mount_values(erroring, {}, {"n": 1})


def test_validate_mount_path_refusals(svc: StatesService) -> None:
    with pytest.raises(MountConflictError, match="must be a list"):
        svc._validate_mount_path("not-a-list")
    with pytest.raises(MountConflictError, match="non-empty object key"):
        svc._validate_mount_path([""])
    with pytest.raises(MountConflictError, match="non-empty object key"):
        svc._validate_mount_path([123])
