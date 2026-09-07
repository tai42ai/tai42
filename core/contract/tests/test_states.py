"""Tests for the subject-keyed state store contract.

Pins the wire-model refusals of the state store (subject charset, empty key,
declaration default-kind membership, ``extra="forbid"``), the JSON-Schema
``schema`` alias round-trip, the completed-vs-consumer origin split, the error
taxonomy, and the ``AppStates`` facet's enumerated surface.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract import ErrorKind, error_kind
from tai42_contract.app.facets import AppStates
from tai42_contract.states import (
    ApplyResult,
    CompletedOrigin,
    ConsumerRow,
    DeclarationInUseError,
    ModuleValidationError,
    RecordView,
    StateContext,
    StateDeclaration,
    StateExistsError,
    StateModuleDocument,
    StateNotFoundError,
    StatesNotConfiguredError,
    StateSubject,
    SubjectCandidates,
    SubjectRefusedError,
    WriteEntry,
    WriteOrigin,
    WritesPage,
)

from ._helpers import protocol_members


def _subject(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"target_kind": "tool", "target_name": "assistant", "kind": "thread", "key": "t-1"}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# StateSubject
# --------------------------------------------------------------------------- #
def test_subject_accepts_a_valid_shape_and_is_frozen():
    subject = StateSubject.model_validate(_subject())
    assert (subject.target_kind, subject.target_name, subject.kind, subject.key) == (
        "tool",
        "assistant",
        "thread",
        "t-1",
    )
    with pytest.raises(ValidationError):
        subject.key = "other"  # type: ignore[misc]


def test_subject_equality_is_all_four_fields():
    base = StateSubject.model_validate(_subject())
    assert base == StateSubject.model_validate(_subject())
    assert base != StateSubject.model_validate(_subject(key="t-2"))
    assert base != StateSubject.model_validate(_subject(kind="person"))
    assert base != StateSubject.model_validate(_subject(target_name="relay"))


@pytest.mark.parametrize("kind", ["Thread", "1thread", "-thread", "thread!", "a" * 64, ""])
def test_subject_rejects_a_kind_outside_the_charset(kind: str):
    with pytest.raises(ValidationError):
        StateSubject.model_validate(_subject(kind=kind))


def test_subject_rejects_an_empty_or_blank_key():
    with pytest.raises(ValidationError):
        StateSubject.model_validate(_subject(key=""))
    with pytest.raises(ValidationError):
        StateSubject.model_validate(_subject(key="   "))


def test_subject_strips_the_key_and_bounds_its_length():
    assert StateSubject.model_validate(_subject(key="  t-1  ")).key == "t-1"
    with pytest.raises(ValidationError):
        StateSubject.model_validate(_subject(key="k" * 513))


def test_subject_rejects_extra_keys_and_an_unknown_target_kind():
    with pytest.raises(ValidationError):
        StateSubject.model_validate(_subject(bogus=1))
    with pytest.raises(ValidationError):
        StateSubject.model_validate(_subject(target_kind="channel"))


# --------------------------------------------------------------------------- #
# StateDeclaration
# --------------------------------------------------------------------------- #
def _decl(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "profile",
        "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        "subject_kinds": ["thread", "person"],
        "default_subject_kind": "thread",
    }
    body.update(overrides)
    return body


def test_declaration_round_trips_the_schema_under_the_wire_key():
    decl = StateDeclaration.model_validate(_decl())
    assert decl.schema_ == {"type": "object", "properties": {"a": {"type": "string"}}}
    dumped = decl.model_dump()
    assert "schema" in dumped
    assert "schema_" not in dumped
    assert StateDeclaration.model_validate(dumped) == decl


@pytest.mark.parametrize("name", ["Profile", "-lead", "has:colon", "café", ""])
def test_declaration_rejects_a_bad_name(name: str):
    with pytest.raises(ValidationError):
        StateDeclaration.model_validate(_decl(name=name))


def test_declaration_requires_at_least_one_unique_subject_kind():
    with pytest.raises(ValidationError):
        StateDeclaration.model_validate(_decl(subject_kinds=[]))
    with pytest.raises(ValidationError):
        StateDeclaration.model_validate(_decl(subject_kinds=["thread", "thread"], default_subject_kind="thread"))


def test_declaration_rejects_a_subject_kind_outside_the_charset():
    with pytest.raises(ValidationError):
        StateDeclaration.model_validate(_decl(subject_kinds=["Thread"], default_subject_kind="Thread"))


def test_declaration_requires_the_default_kind_to_be_declared():
    with pytest.raises(ValidationError) as excinfo:
        StateDeclaration.model_validate(_decl(default_subject_kind="agent"))
    assert "default_subject_kind" in str(excinfo.value)


def test_declaration_rejects_a_nonpositive_retention_and_extra_keys():
    with pytest.raises(ValidationError):
        StateDeclaration.model_validate(_decl(retention_days=0))
    with pytest.raises(ValidationError):
        StateDeclaration.model_validate(_decl(bogus=1))
    assert StateDeclaration.model_validate(_decl(retention_days=30)).retention_days == 30


def test_declaration_carries_the_platform_composed_effective_schema():
    served = StateDeclaration.model_validate(
        _decl(effective_schema={"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "number"}}})
    )
    assert served.effective_schema == {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
    }
    assert StateDeclaration.model_validate(_decl()).effective_schema is None
    assert "effective_schema" in served.model_dump()


def test_declaration_carries_the_platform_composed_regimes():
    # ``regimes`` are the absolute write-regime rules the platform composes over the
    # mounts and serves on every read; a plain declaration carries none (None), and the
    # served field round-trips the composed list.
    served = StateDeclaration.model_validate(_decl(regimes=[{"path": ["sub", "tags"], "regime": "composing"}]))
    assert served.regimes == [{"path": ["sub", "tags"], "regime": "composing"}]
    assert StateDeclaration.model_validate(_decl()).regimes is None
    assert "regimes" in served.model_dump()


# --------------------------------------------------------------------------- #
# StateModuleDocument
# --------------------------------------------------------------------------- #
def test_module_document_defaults_kind_and_round_trips_schema():
    doc = StateModuleDocument.model_validate({"name": "agenda", "schema": {"type": "object"}})
    assert doc.kind == "state-module"
    assert doc.schema_ == {"type": "object"}
    assert "schema" in doc.model_dump()


def test_module_document_refuses_any_key_outside_the_platform_set():
    # A consumer keeps its own keys in its own sibling document; the platform module
    # document forbids anything outside its set so a mis-split fails loudly at the wire.
    for key in ("views", "widgets", "anything"):
        with pytest.raises(ValidationError):
            StateModuleDocument.model_validate({"name": "agenda", "schema": {}, key: {}})


@pytest.mark.parametrize("name", ["Agenda", "1agenda", "has_underscore", "a" * 64])
def test_module_document_rejects_a_bad_name(name: str):
    with pytest.raises(ValidationError):
        StateModuleDocument.model_validate({"name": name, "schema": {}})


# --------------------------------------------------------------------------- #
# Origins, context, and the read/apply/audit shapes
# --------------------------------------------------------------------------- #
def test_write_origin_carries_only_consumer_facts():
    origin = WriteOrigin(consumer="agenda", meta={"node": "n1"}, run_id="r1", op_id="e:agenda:k")
    # A consumer cannot supply door/actor/turn_id — they are absent from its model.
    with pytest.raises(ValidationError):
        WriteOrigin.model_validate({"consumer": "agenda", "door": "hook"})
    assert origin.consumer == "agenda"
    # ``meta`` is an opaque bag stored and echoed verbatim — the platform reads no key from it.
    assert origin.meta == {"node": "n1"}


def test_write_origin_meta_is_bounded_and_must_be_a_json_object():
    from tai42_contract.states.models import MAX_ORIGIN_META_BYTES

    # A bag past the byte bound is refused loudly, naming the limit.
    oversized = {"blob": "x" * (MAX_ORIGIN_META_BYTES + 1)}
    with pytest.raises(ValidationError, match=str(MAX_ORIGIN_META_BYTES)):
        WriteOrigin(meta=oversized)
    # A non-JSON-serializable value is refused as not a JSON object.
    with pytest.raises(ValidationError, match="JSON object"):
        WriteOrigin(meta={"bad": {1, 2}})


def test_completed_origin_adds_the_platform_stamped_fields():
    completed = CompletedOrigin(consumer="agenda", door="hook", actor="k-fire", turn_id="t9", inbound_id="in-1")
    assert (completed.door, completed.actor, completed.turn_id, completed.inbound_id) == (
        "hook",
        "k-fire",
        "t9",
        "in-1",
    )
    with pytest.raises(ValidationError):
        CompletedOrigin.model_validate({"door": "nope"})


def test_state_context_pins_the_door_literal_and_is_frozen():
    ctx = StateContext(
        door="conversation",
        candidates=SubjectCandidates(target_kind="tool", target_name="assistant", by_kind={"thread": "t-1"}),
    )
    assert ctx.candidates.by_kind["thread"] == "t-1"
    with pytest.raises(ValidationError):
        ctx.actor = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        StateContext.model_validate({"door": "email", "candidates": ctx.candidates.model_dump()})


def test_record_apply_and_write_entry_shapes():
    subject = StateSubject.model_validate(_subject())
    view = RecordView(state="profile", subject=subject, data={"a": "b"}, seq=1.0, canonical_subject=subject)
    assert view.folded_from == []
    result = ApplyResult(applied=True, data={"a": "b"}, seq=2.0)
    assert result.skipped == []
    entry = WriteEntry.model_validate(
        {
            "seq": 2.0,
            "at": "2026-09-06T00:00:00Z",
            "origin": {"door": "api", "actor": "admin"},
            "paths": [["a"], ["items", 0, "id"]],
        }
    )
    assert entry.paths[1] == ["items", 0, "id"]
    assert entry.origin.door == "api"


def test_writes_page_wraps_items_and_a_keyset_cursor():
    page = WritesPage(
        items=[
            WriteEntry.model_validate(
                {"seq": 1.0, "at": "2026-09-06T00:00:00Z", "origin": {"door": "api"}, "paths": [["a"]]}
            )
        ],
        next_cursor="42",
    )
    assert page.next_cursor == "42"
    assert page.items[0].origin.door == "api"
    # An exhausted page carries no cursor, and the shape forbids a typo'd key.
    assert WritesPage().next_cursor is None
    with pytest.raises(ValidationError):
        WritesPage.model_validate({"items": [], "next_cursor": None, "matches": []})


def test_declaration_served_updated_at_is_optional_and_refused_shape_stays_forbid():
    # ``updated_at`` is a served-only timestamp: absent on a client write, present on a read.
    assert StateDeclaration(name="s", subject_kinds=["thread"], default_subject_kind="thread").updated_at is None
    served = StateDeclaration(
        name="s",
        subject_kinds=["thread"],
        default_subject_kind="thread",
        updated_at="2026-09-06T00:00:00Z",  # type: ignore[arg-type]
    )
    assert served.updated_at is not None


def test_consumer_row_allows_an_unavailable_marker():
    assert ConsumerRow(kind="flow", name="agenda", detail="binds thread").name == "agenda"
    assert ConsumerRow(kind="schedule", unavailable="no scheduling backend").unavailable


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (StatesNotConfiguredError("off"), ErrorKind.UNAVAILABLE),
        (StateNotFoundError("x"), ErrorKind.NOT_FOUND),
        (StateExistsError("x"), ErrorKind.CONFLICT),
        (SubjectRefusedError("bad"), ErrorKind.BAD_INPUT),
        (DeclarationInUseError("x"), ErrorKind.CONFLICT),
        (ModuleValidationError("x"), ErrorKind.BAD_INPUT),
    ],
)
def test_errors_stamp_their_transport_neutral_kind(exc: Exception, kind: ErrorKind):
    assert error_kind(exc) is kind


# --------------------------------------------------------------------------- #
# The facet surface
# --------------------------------------------------------------------------- #
def test_appstates_enumerates_its_thirty_four_members():
    members = protocol_members(AppStates)
    assert members == {
        "list_declarations",
        "get_declaration",
        "put_declaration",
        "delete_declaration",
        "stats",
        "migrate",
        "preview_migrate",
        "list_modules",
        "get_module",
        "put_module",
        "delete_module",
        "list_mounts",
        "mount",
        "update_mount_declarations",
        "unmount",
        "import_aliases",
        "import_applied_ops",
        "import_records",
        "read",
        "replace",
        "merge",
        "apply",
        "erase",
        "fold",
        "list_subjects",
        "search",
        "writes",
        "prune_expired",
        "context",
        "register_consumer_lister",
        "consumers",
        "register_module_seed",
        "register_retired_module_name",
        "register_mount_validator",
    }
