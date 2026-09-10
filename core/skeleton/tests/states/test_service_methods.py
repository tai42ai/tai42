"""The states service's record, module and mount doors, plus the pure schema/parameter
validators — driven against the in-memory ``FakeStatesStore`` (no live database). The
declaration lifecycle, gate, subject validation and write-provenance chokepoint are pinned
in ``test_service.py``; this file covers every remaining service branch that the real-store
integration test otherwise exercises.
"""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import (
    AttachConflictError,
    InvalidPathError,
    RegimeViolationError,
    SchemaValidationError,
    StateNotFoundError,
    SubjectFoldError,
    SubjectRefusedError,
    TemplateExistsError,
    TemplateInUseError,
    TemplateValidationError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    AttachBody,
    StateTemplateDocument,
    WriteOrigin,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import (
    StatesService,
    _page_limit,
    _validate_document,
    _validate_schema,
)
from tai42_skeleton.states.templates import validate_template

from .test_service import _STATE, FakeStatesStore, _subject

_ORIGIN = WriteOrigin(consumer="c")


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


def _module_doc(name="mod", **over):
    body = {
        "kind": "state-template",
        "name": name,
        "schema": {"type": "object", "properties": {"y": {"type": "integer"}}},
    }
    body.update(over)
    return StateTemplateDocument.model_validate(body)


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
    await svc.put_template(_module_doc("m1"), replace=False)
    listed = await svc.list_templates()
    assert [m.name for m in listed] == ["m1"]
    got = await svc.get_template("m1")
    assert got is not None
    assert got.name == "m1"
    assert await svc.get_template("absent") is None


async def test_put_module_without_replace_refuses_existing(svc: StatesService) -> None:
    await svc.put_template(_module_doc("m"), replace=False)
    with pytest.raises(TemplateExistsError, match="already exists"):
        await svc.put_template(_module_doc("m"), replace=False)


async def test_put_module_replace_revalidates_live_mounts(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_module_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    # a replace with a still-compatible body backfills the mount's parameters and succeeds
    await svc.put_template(
        _module_doc("m", schema={"type": "object", "properties": {"y": {"type": "integer"}, "z": {"type": "string"}}}),
        replace=True,
    )
    got = await svc.get_template("m")
    assert got is not None
    assert "z" in got.schema_["properties"]


async def test_put_module_replace_refused_when_a_validator_now_rejects(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_module_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))

    async def refusing(doc, declarations, effective) -> None:
        raise TemplateValidationError("no longer valid on this mount")

    svc.register_attach_validator(refusing)
    with pytest.raises(TemplateInUseError, match="no longer validates"):
        await svc.put_template(_module_doc("m"), replace=True)


async def test_delete_module_paths(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no template"):
        await svc.delete_template("absent")
    await svc.put_declaration(_STATE)
    await svc.put_template(_module_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    with pytest.raises(TemplateInUseError, match="attached on state"):
        await svc.delete_template("m")
    await svc.detach("alerts", "m")
    await svc.delete_template("m")
    assert await svc.get_template("m") is None


# --------------------------------------------------------------------------- #
# mounts                                                                        #
# --------------------------------------------------------------------------- #
async def test_list_mounts_every_form(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_module_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    assert len(await svc.list_attachments("alerts", template="m")) == 1
    assert await svc.list_attachments("alerts", template="absent") == []
    assert len(await svc.list_attachments("alerts")) == 1
    assert len(await svc.list_attachments(template="m")) == 1
    assert len(await svc.list_attachments()) == 1


async def test_mount_refuses_a_duplicate(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_module_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    with pytest.raises(AttachConflictError, match="already attached"):
        await svc.attach("alerts", "m", AttachBody(path=["other"]))


async def test_mount_missing_module_raises(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(StateNotFoundError, match="no template"):
        await svc.attach("alerts", "absent", AttachBody(path=["sub"]))


def _capped_module(name: str = "capped"):
    """A module whose declarations ``check`` constrains a declared ``count`` against the
    mount's effective ``limit`` parameter, read as ``$parameters.limit``."""
    return _module_doc(
        name,
        schema={"type": "object", "properties": {"box": {"type": "object"}}},
        parameters={"limit": {"schema": {"type": "integer"}, "default": 5}},
        declarations={
            "schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
            "check": 'if .count <= $parameters.limit then true else "count exceeds the mount limit" end',
        },
    )


async def test_mount_check_reads_effective_parameters(svc: StatesService) -> None:
    """A declarations check reads the mount's supplied parameters as ``$parameters``: a
    declaration within the supplied ``limit`` mounts, one exceeding it is refused with the
    check's message."""
    await svc.put_declaration(_STATE)
    await svc.put_template(_capped_module(), replace=False)
    with pytest.raises(TemplateValidationError, match="count exceeds the mount limit"):
        await svc.attach("alerts", "capped", AttachBody(path=["a"], parameters={"limit": 8}, declarations={"count": 9}))
    await svc.attach("alerts", "capped", AttachBody(path=["a"], parameters={"limit": 8}, declarations={"count": 7}))


async def test_mount_check_sees_the_parameter_default(svc: StatesService) -> None:
    """A mount supplying no ``limit`` sees the module default (5) in the check, so the check
    constrains against the same value the runtime persists."""
    await svc.put_declaration(_STATE)
    await svc.put_template(_capped_module(), replace=False)
    with pytest.raises(TemplateValidationError, match="count exceeds the mount limit"):
        await svc.attach("alerts", "capped", AttachBody(path=["a"], declarations={"count": 6}))
    await svc.attach("alerts", "capped", AttachBody(path=["a"], declarations={"count": 4}))


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
    await svc.put_template(decl_module, replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"], declarations={"n": 1}))
    await svc.update_attachment_declarations("alerts", "m", {"n": 2})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert store.mounts[("alerts", "m")]["declarations"] == {"n": 2}
    with pytest.raises(StateNotFoundError, match="not attached"):
        await svc.update_attachment_declarations("alerts", "absent", {})


async def test_unmount_missing_raises(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(StateNotFoundError, match="not attached"):
        await svc.detach("alerts", "absent")


# --------------------------------------------------------------------------- #
# mount reconcilers                                                            #
# --------------------------------------------------------------------------- #
async def _mount_module(svc: StatesService, *, declarations: dict, options: dict | None = None) -> None:
    decl_module = _module_doc(
        "m", declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}
    )
    await svc.put_template(decl_module, replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"], declarations=declarations, options=options or {}))


async def test_mount_runs_reconciler_with_a_first_mount_context(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    seen = []
    svc.register_attach_reconciler(lambda ctx: seen.append(ctx) or _noop())
    await _mount_module(svc, declarations={"n": 1}, options={"on_orphan": "close"})
    (ctx,) = seen
    assert ctx.state == "alerts"
    assert ctx.template.name == "m"
    assert ctx.operation == "attach"
    assert ctx.previous_declarations is None
    assert ctx.new_declarations == {"n": 1}
    assert ctx.options == {"on_orphan": "close"}


async def test_update_declarations_runs_reconciler_with_previous_and_options(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await _mount_module(svc, declarations={"n": 1})
    seen = []
    svc.register_attach_reconciler(lambda ctx: seen.append(ctx) or _noop())
    await svc.update_attachment_declarations("alerts", "m", {"n": 2}, options={"on_orphan": "refuse"})
    (ctx,) = seen
    assert ctx.operation == "update_declarations"
    assert ctx.previous_declarations == {"n": 1}
    assert ctx.new_declarations == {"n": 2}
    assert ctx.options == {"on_orphan": "refuse"}
    # options are a per-operation directive passed to the reconciler, never stored.
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "options" not in store.mounts[("alerts", "m")]


async def test_raising_reconciler_refuses_the_mount_and_writes_nothing(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)

    async def _refuse(ctx):
        raise TemplateValidationError("record t1 points at a value the new declarations drop")

    svc.register_attach_reconciler(_refuse)
    with pytest.raises(TemplateValidationError, match="points at a value"):
        await _mount_module(svc, declarations={"n": 1})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert ("alerts", "m") not in store.mounts


async def test_reconciler_writes_are_visible_after_the_mount_commits(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)

    async def _close_open(ctx):
        page = await ctx.records.list_subjects()
        for sub in page["subjects"]:
            subject = _subject(kind=sub["subject"]["kind"], key=sub["subject"]["key"])
            await ctx.records.merge(subject, {"closed": True}, origin=WriteOrigin(consumer="reconciler"))

    svc.register_attach_reconciler(_close_open)
    await _mount_module(svc, declarations={"n": 1})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert ("alerts", "m") in store.mounts
    view = await svc.read("alerts", _subject(key="open1"))
    assert view is not None
    assert view.data["closed"] is True


async def test_reconciler_merge_then_raise_rolls_back_the_record_and_the_mount(svc: StatesService) -> None:
    # The reconcile + mount write share ONE transaction: a reconciler that writes a record
    # and then refuses leaves NEITHER the record write NOR the mount — atomic all-or-nothing.
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)

    async def _write_then_refuse(ctx):
        await ctx.records.merge(_subject(key="open1"), {"closed": True}, origin=WriteOrigin(consumer="reconciler"))
        raise TemplateValidationError("record open1 points at a value the new declarations drop")

    svc.register_attach_reconciler(_write_then_refuse)
    with pytest.raises(TemplateValidationError, match="points at a value"):
        await _mount_module(svc, declarations={"n": 1})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert ("alerts", "m") not in store.mounts
    view = await svc.read("alerts", _subject(key="open1"))
    assert view is not None
    assert "closed" not in view.data


async def test_failed_mount_write_rolls_back_a_reconciler_write(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A merge succeeds inside the reconciler, then the mount write itself fails: the shared
    # transaction rolls the reconciler's record write back too.
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)

    async def _close_open(ctx):
        await ctx.records.merge(_subject(key="open1"), {"closed": True}, origin=WriteOrigin(consumer="reconciler"))

    svc.register_attach_reconciler(_close_open)

    async def _boom(*args, **kwargs):
        raise RuntimeError("mount write failed")

    monkeypatch.setattr(svc._store, "upsert_attachment", _boom)
    with pytest.raises(RuntimeError, match="mount write failed"):
        await _mount_module(svc, declarations={"n": 1})
    view = await svc.read("alerts", _subject(key="open1"))
    assert view is not None
    assert "closed" not in view.data


async def test_skip_reconcilers_runs_validators_but_not_reconcilers(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    calls: list[str] = []

    async def _validator(module_doc, declarations, effective):
        calls.append("validator")

    async def _reconciler(ctx):
        calls.append("reconciler")

    svc.register_attach_validator(_validator)
    svc.register_attach_reconciler(_reconciler)
    decl_module = _module_doc(
        "m", declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}
    )
    await svc.put_template(decl_module, replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"], declarations={"n": 1}), skip_reconcilers=True)
    assert calls == ["validator"]


async def test_reconciler_reads_its_own_in_flight_merge(svc: StatesService) -> None:
    # The record door's read/list_subjects run on the mount transaction, so a reconciler
    # sees the merge it just wrote (read-your-writes) before the mount commits.
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)
    seen: list[object] = []

    async def _read_own_write(ctx):
        await ctx.records.merge(_subject(key="open1"), {"n": 9}, origin=WriteOrigin(consumer="reconciler"))
        view = await ctx.records.read(_subject(key="open1"))
        seen.append(None if view is None else view.data.get("n"))
        page = await ctx.records.list_subjects()
        seen.append(len(page["subjects"]))

    svc.register_attach_reconciler(_read_own_write)
    await _mount_module(svc, declarations={"n": 1})
    assert seen == [9, 1]


_COMPOSING_MODULE = {
    "schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "closed": {"type": "boolean"}},
                },
            }
        },
    },
    "regimes": [{"path": ["entries"], "regime": "composing"}],
}


async def _setup_composing_ledger(store: object, monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    rsvc = StatesService(store=store)  # type: ignore[arg-type]
    await rsvc.put_declaration(_STATE)
    await rsvc.put_template(_module_doc("cmod", **_COMPOSING_MODULE), replace=False)
    await rsvc.attach("alerts", "cmod", AttachBody(path=[], declarations={}))
    await rsvc.apply(
        "alerts",
        _subject(key="led1"),
        [{"op": "set_by_key", "path": ["entries"], "key_field": "id", "value": {"id": 1}}],
        op_id=None,
        origin=_ORIGIN,
    )
    return rsvc


async def test_reconciler_apply_closes_a_composing_record_that_merge_cannot(
    pg: object, store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A record under a ``composing`` write regime cannot be closed with ``merge`` (a
    # whole-path set is a RegimeViolationError); a reconciler closes it with the keyed
    # ``apply`` — exactly what a module fill can write — and the mount commits it.
    rsvc = await _setup_composing_ledger(store, monkeypatch)
    subject = _subject(key="led1")

    async def _close(ctx):
        with pytest.raises(RegimeViolationError):
            await ctx.records.merge(subject, {"entries": [{"id": 1, "closed": True}]}, origin=_ORIGIN)
        await ctx.records.apply(
            subject,
            [{"op": "set_by_key", "path": ["entries"], "key_field": "id", "value": {"id": 1, "closed": True}}],
            origin=WriteOrigin(consumer="reconciler"),
        )

    rsvc.register_attach_reconciler(_close)
    await rsvc.update_attachment_declarations("alerts", "cmod", {})
    view = await rsvc.read("alerts", subject)
    assert view is not None
    assert view.data["entries"] == [{"id": 1, "closed": True}]


async def test_reconciler_apply_on_a_composing_record_rolls_back_on_refuse(
    pg: object, store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rsvc = await _setup_composing_ledger(store, monkeypatch)
    subject = _subject(key="led1")

    async def _close_then_refuse(ctx):
        await ctx.records.apply(
            subject,
            [{"op": "set_by_key", "path": ["entries"], "key_field": "id", "value": {"id": 1, "closed": True}}],
            origin=WriteOrigin(consumer="reconciler"),
        )
        raise TemplateValidationError("refuse after the keyed write")

    rsvc.register_attach_reconciler(_close_then_refuse)
    with pytest.raises(TemplateValidationError, match="refuse after the keyed write"):
        await rsvc.update_attachment_declarations("alerts", "cmod", {})
    view = await rsvc.read("alerts", subject)
    assert view is not None
    assert view.data["entries"] == [{"id": 1}]  # the keyed write rolled back with the refused mount


async def test_reconciler_runs_after_the_validator(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    order: list[str] = []

    async def _validator(module_doc, declarations, effective):
        order.append("validator")

    async def _reconciler(ctx):
        order.append("reconciler")

    svc.register_attach_validator(_validator)
    svc.register_attach_reconciler(_reconciler)
    await _mount_module(svc, declarations={"n": 1})
    assert order == ["validator", "reconciler"]


async def _noop() -> None:
    return None


def test_regime_for_path(svc: StatesService) -> None:
    module = validate_template(
        {
            "kind": "state-template",
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
    svc.register_template_seed(_module_doc("seeded"))
    await svc.apply_template_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "seeded" in store.modules


async def test_apply_module_seeds_is_a_noop_when_feature_off(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.register_template_seed(_module_doc("seeded"))
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: False)
    await svc.apply_template_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "seeded" not in store.modules


async def test_module_cache_serves_hit_and_evicts(svc: StatesService) -> None:
    svc._TEMPLATE_CACHE_MAX = 1  # type: ignore[misc]
    await svc.put_template(_module_doc("m1"), replace=False)
    await svc.put_template(_module_doc("m2"), replace=False)
    await svc.get_template("m1")  # populates the cache
    await svc.get_template("m1")  # a cache hit (move-to-end)
    await svc.get_template("m2")  # overflows the bounded cache → eviction
    assert len(svc._template_cache) == 1


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
    param_module = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"cap": {"$parameter": "cap"}}},
            "parameters": {"cap": {"schema": {"type": "integer"}}},
        }
    )
    with pytest.raises(TemplateValidationError, match="unknown parameter"):
        await svc._validate_attach_values(param_module, {"nope": 1}, {})
    with pytest.raises(TemplateValidationError, match="is invalid"):
        await svc._validate_attach_values(param_module, {"cap": "not-an-int"}, {})
    with pytest.raises(TemplateValidationError, match="must supply parameter"):
        await svc._validate_attach_values(param_module, {}, {})


async def test_validate_mount_values_declaration_branches(svc: StatesService) -> None:
    plain = validate_template(
        {"kind": "state-template", "name": "m", "schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
    )
    with pytest.raises(TemplateValidationError, match="declares no declarations section"):
        await svc._validate_attach_values(plain, {}, {"x": 1})

    checked = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}, "check": ".n > 0"},
        }
    )
    with pytest.raises(TemplateValidationError, match="invalid under template"):
        await svc._validate_attach_values(checked, {}, {"n": "bad"})
    with pytest.raises(TemplateValidationError, match="rejected by template"):
        await svc._validate_attach_values(checked, {}, {"n": -1})
    # a passing declaration set clears every gate (the happy path through the check)
    await svc._validate_attach_values(checked, {}, {"n": 3})


async def test_validate_mount_values_check_string_message_and_eval_error(svc: StatesService) -> None:
    stringy = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "check": 'if .n > 0 then true else "n must be positive" end',
            },
        }
    )
    with pytest.raises(TemplateValidationError, match="n must be positive"):
        await svc._validate_attach_values(stringy, {}, {"n": -1})

    erroring = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "check": '.n | error("boom")',
            },
        }
    )
    with pytest.raises(TemplateValidationError, match="failed to evaluate"):
        await svc._validate_attach_values(erroring, {}, {"n": 1})


def test_validate_mount_path_refusals(svc: StatesService) -> None:
    with pytest.raises(AttachConflictError, match="must be a list"):
        svc._validate_attach_path("not-a-list")
    with pytest.raises(AttachConflictError, match="non-empty object key"):
        svc._validate_attach_path([""])
    with pytest.raises(AttachConflictError, match="non-empty object key"):
        svc._validate_attach_path([123])
