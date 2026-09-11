"""The states service's ``template_jq`` programs (input eval + update apply), name
resolution across attachments, and the built-in template-document reconciler — driven
against the in-memory ``FakeStatesStore`` (no live database). The op_id idempotency and the
composing-regime refusal that need real Postgres semantics are pinned in
``test_store_integration.py``; this file covers the evaluate-and-dispatch logic.
"""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import (
    StateNotFoundError,
    TemplateValidationError,
    ValueValidationError,
)
from tai42_contract.states.models import AttachBody, StateTemplateDocument, WriteOrigin

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .test_service import _STATE, FakeStatesStore, _subject

_ORIGIN = WriteOrigin(consumer="c")

_TEMPLATE = StateTemplateDocument.model_validate(
    {
        "name": "summary",
        "schema": {
            "type": "object",
            "properties": {
                "ledger": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
            },
        },
        "template_jq": {
            # input-purpose programs read the record and return a value
            "anything_due": {"purpose": "input", "jq": "(.ledger // []) | length > 0"},
            "ids": {"purpose": "input", "description": "the ledger ids", "jq": "[(.ledger // [])[] | .id]"},
            "one": {
                "purpose": "input",
                "params": ["id"],
                "jq": "[(.ledger // [])[] | select(.id == $params.id)] | first",
            },
            "count": {"purpose": "input", "jq": "(.ledger // []) | length"},
            "due": {"purpose": "input", "jq": "tjq_anything_due({})"},  # input calling a sibling input
            # update-purpose programs map {record, input} to an op batch
            "add": {
                "purpose": "update",
                "writes": [["ledger"]],
                "jq": '[{op: "set", path: ["ledger"], value: ((.record.ledger // []) + [.input])}]',
            },
            "bad_shape": {"purpose": "update", "writes": [["ledger"]], "jq": '"not a list"'},
        },
    }
)


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def _attached(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_TEMPLATE, replace=False)
    await svc.attach(_STATE.name, "summary", AttachBody(path=[]))


async def _seed_ledger(svc: StatesService, items: list[dict[str, str]]) -> None:
    await svc.replace(_STATE.name, _subject(), {"ledger": items}, origin=_ORIGIN)


# --------------------------------------------------------------------------- #
# input programs (eval)                                                         #
# --------------------------------------------------------------------------- #
async def test_input_program_returns_a_value_over_the_record(svc: StatesService) -> None:
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}, {"id": "b"}])
    result = await svc.eval_template_jq(_STATE.name, _subject(), "anything_due", {})
    assert result.name == "anything_due"
    assert result.purpose == "input"
    assert result.value is True
    ids = await svc.eval_template_jq(_STATE.name, _subject(), "ids", {})
    assert ids.value == ["a", "b"]


async def test_input_program_takes_declared_params(svc: StatesService) -> None:
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}, {"id": "b"}])
    result = await svc.eval_template_jq(_STATE.name, _subject(), "one", {"id": "b"})
    assert result.value == {"id": "b"}


async def test_input_program_on_an_empty_record_is_not_an_error(svc: StatesService) -> None:
    await _attached(svc)
    result = await svc.eval_template_jq(_STATE.name, _subject(), "anything_due", {})
    assert result.value is False


async def test_input_program_returns_a_value_without_writing(svc: StatesService) -> None:
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}, {"id": "b"}])
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    before = len(store.applied_origins)
    result = await svc.eval_template_jq(_STATE.name, _subject(), "count", {})
    assert result.value == 2
    # An input program never reaches the store's apply — nothing is written.
    assert len(store.applied_origins) == before


async def test_input_program_may_call_a_sibling_input_program(svc: StatesService) -> None:
    # ``due``'s jq is ``anything_due`` — it calls the sibling input program by name.
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}])
    result = await svc.eval_template_jq(_STATE.name, _subject(), "due", {})
    assert result.value is True


async def test_unknown_program_is_a_not_found(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(StateNotFoundError, match="no template_jq 'nope'"):
        await svc.eval_template_jq(_STATE.name, _subject(), "nope", {})


async def test_undeclared_param_is_refused(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(ValueValidationError, match="unknown"):
        await svc.eval_template_jq(_STATE.name, _subject(), "anything_due", {"stray": 1})


async def test_eval_refuses_an_update_purpose_program(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(ValueValidationError, match="eval needs an 'input'-purpose"):
        await svc.eval_template_jq(_STATE.name, _subject(), "add", {})


# --------------------------------------------------------------------------- #
# update programs (apply)                                                       #
# --------------------------------------------------------------------------- #
async def test_update_program_applies_through_the_store(svc: StatesService) -> None:
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}])
    result = await svc.apply_template_jq(_STATE.name, _subject(), "add", {"id": "b"}, op_id=None, origin=_ORIGIN)
    assert result.name == "add"
    assert result.applied is True
    view = await svc.read(_STATE.name, _subject())
    assert view is not None
    assert view.data["ledger"] == [{"id": "a"}, {"id": "b"}]


async def test_update_program_returning_a_non_list_is_refused(svc: StatesService) -> None:
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}])
    with pytest.raises(ValueValidationError, match="must return an op batch"):
        await svc.apply_template_jq(_STATE.name, _subject(), "bad_shape", None, op_id=None, origin=_ORIGIN)


async def test_unknown_update_program_is_a_not_found(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(StateNotFoundError, match="no template_jq 'nope'"):
        await svc.apply_template_jq(_STATE.name, _subject(), "nope", None, op_id=None, origin=_ORIGIN)


async def test_apply_refuses_an_input_purpose_program(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(ValueValidationError, match="apply needs an 'update'-purpose"):
        await svc.apply_template_jq(_STATE.name, _subject(), "anything_due", None, op_id=None, origin=_ORIGIN)


_PARAMS_TEMPLATE = StateTemplateDocument.model_validate(
    {
        "name": "summary",
        "schema": {
            "type": "object",
            "properties": {
                "ledger": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
            },
        },
        "template_jq": {
            "put": {
                "purpose": "update",
                "params": ["id", "label"],
                "writes": [["ledger"]],
                "jq": '[{op: "set", path: ["ledger"], value: ((.record.ledger // []) + [.input])}]',
            }
        },
    }
)


async def _attached_params(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_PARAMS_TEMPLATE, replace=False)
    await svc.attach(_STATE.name, "summary", AttachBody(path=[]))


async def test_update_with_declared_params_validates_the_input_object(svc: StatesService) -> None:
    await _attached_params(svc)
    # Exact keys apply (a value may be null).
    ok = await svc.apply_template_jq(
        _STATE.name, _subject(), "put", {"id": "a", "label": None}, op_id=None, origin=_ORIGIN
    )
    assert ok.applied is True
    # A missing declared key is a loud 422.
    with pytest.raises(ValueValidationError, match="input missing"):
        await svc.apply_template_jq(_STATE.name, _subject(), "put", {"id": "a"}, op_id=None, origin=_ORIGIN)
    # An undeclared key is a loud 422.
    with pytest.raises(ValueValidationError, match="undeclared"):
        await svc.apply_template_jq(
            _STATE.name, _subject(), "put", {"id": "a", "label": "x", "extra": 1}, op_id=None, origin=_ORIGIN
        )
    # A non-object input against a params-declaring update is a loud 422.
    with pytest.raises(ValueValidationError, match="input must be an object"):
        await svc.apply_template_jq(_STATE.name, _subject(), "put", "nope", op_id=None, origin=_ORIGIN)


async def test_apply_origin_carries_the_template_jq_name(svc: StatesService) -> None:
    await _attached(svc)
    await _seed_ledger(svc, [{"id": "a"}])
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    await svc.apply_template_jq(
        _STATE.name, _subject(), "add", {"id": "b"}, op_id=None, origin=WriteOrigin(meta={"template_jq": "add"})
    )
    # The completed origin the store recorded carries the generic ``{"template_jq": ...}`` provenance.
    assert store.applied_origins[-1].meta == {"template_jq": "add"}


# --------------------------------------------------------------------------- #
# name resolution across attachments                                            #
# --------------------------------------------------------------------------- #
async def test_program_declared_by_two_templates_is_ambiguous_and_qualifies(svc: StatesService) -> None:
    dup = {"a": {"purpose": "input", "jq": "1"}}
    m1 = StateTemplateDocument.model_validate(
        {"name": "one", "schema": {"type": "object", "properties": {"p": {"type": "integer"}}}, "template_jq": dup}
    )
    m2 = StateTemplateDocument.model_validate(
        {"name": "two", "schema": {"type": "object", "properties": {"q": {"type": "integer"}}}, "template_jq": dup}
    )
    await svc.put_declaration(_STATE)
    await svc.put_template(m1, replace=False)
    await svc.put_template(m2, replace=False)
    await svc.attach(_STATE.name, "one", AttachBody(path=["x"]))
    await svc.attach(_STATE.name, "two", AttachBody(path=["y"]))
    # An unqualified name two attached templates declare is ambiguous.
    with pytest.raises(ValueValidationError, match="more than one template"):
        await svc.eval_template_jq(_STATE.name, _subject(), "a", {})
    # The qualified ``<template>.<name>`` form resolves it to the named attachment.
    result = await svc.eval_template_jq(_STATE.name, _subject(), "one.a", {})
    assert result.name == "one.a"
    assert result.value == 1


async def test_qualified_name_on_an_unattached_template_is_a_not_found(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(StateNotFoundError, match="'ghost' is not attached"):
        await svc.eval_template_jq(_STATE.name, _subject(), "ghost.anything_due", {})


async def test_qualified_unknown_program_on_an_attached_template_is_a_not_found(svc: StatesService) -> None:
    await _attached(svc)
    with pytest.raises(StateNotFoundError, match="declares no template_jq 'nope'"):
        await svc.eval_template_jq(_STATE.name, _subject(), "summary.nope", {})


# --------------------------------------------------------------------------- #
# the built-in template-document reconciler                                     #
# --------------------------------------------------------------------------- #
_RECON_TEMPLATE = StateTemplateDocument.model_validate(
    {
        "name": "reconciler",
        "schema": {
            "type": "object",
            "properties": {
                "ledger": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
            },
        },
        "declarations": {"schema": {"type": "object", "properties": {"allowed": {"type": "array"}}}},
        "reconcile": {
            "orphans": (
                ".new.allowed as $a|[(.data.ledger//[])[]|select(.id as $i|($a|index($i))==null)|{id,label:.id}]"
            ),
            "resolutions": '["closed"]',
            "close": '[{op: "set", path: ["ledger"], value: []}]',
        },
    }
)


async def _attached_reconciler(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_RECON_TEMPLATE, replace=False)
    await svc.attach(_STATE.name, "reconciler", AttachBody(path=[], declarations={"allowed": ["a", "c"]}))
    await svc.replace(_STATE.name, _subject(), {"ledger": [{"id": "a"}, {"id": "c"}]}, origin=_ORIGIN)


async def test_reconcile_refuses_a_declarations_edit_that_orphans(svc: StatesService) -> None:
    await _attached_reconciler(svc)
    with pytest.raises(TemplateValidationError, match="would orphan") as excinfo:
        await svc.update_attachment_declarations(_STATE.name, "reconciler", {"allowed": ["a"]})
    # The refusal carries the orphan list as STRUCTURED data (not only prose), so the UI
    # keys its resolve step on ``extra.orphans`` rather than matching the message.
    extra = excinfo.value.extra
    assert extra is not None
    assert extra["reconcile"] is True
    assert [o["id"] for o in extra["orphans"]] == ["c"]
    # The refused declarations edit rolled back — the attachment still declares the old set.
    attachments = await svc.list_attachments(_STATE.name, template="reconciler")
    assert attachments[0]["declarations"] == {"allowed": ["a", "c"]}


def test_states_door_propagates_structured_extra_to_the_operation_error() -> None:
    # The one seam every states op runs through carries a StatesError's structured ``extra``
    # onto the mapped operation error, so a 422 body exposes the orphan payload.
    from tai42_skeleton.operations.errors import ValidationRejected
    from tai42_skeleton.operations.states import _states_door

    payload = {"reconcile": True, "orphans": [{"subject": "s", "id": "c"}]}
    with pytest.raises(ValidationRejected) as excinfo, _states_door():
        raise TemplateValidationError("would orphan …", extra=payload)
    assert excinfo.value.extra == payload


async def test_reconcile_closes_orphans_with_the_close_directive(svc: StatesService) -> None:
    await _attached_reconciler(svc)
    await svc.update_attachment_declarations(
        _STATE.name, "reconciler", {"allowed": ["a"]}, options={"orphans": "close", "resolution": "closed"}
    )
    attachments = await svc.list_attachments(_STATE.name, template="reconciler")
    assert attachments[0]["declarations"] == {"allowed": ["a"]}
    view = await svc.read(_STATE.name, _subject())
    assert view is not None
    assert view.data["ledger"] == []


async def test_reconcile_close_refuses_an_undeclared_resolution(svc: StatesService) -> None:
    await _attached_reconciler(svc)
    with pytest.raises(TemplateValidationError, match="not a not-done resolution"):
        await svc.update_attachment_declarations(
            _STATE.name, "reconciler", {"allowed": ["a"]}, options={"orphans": "close", "resolution": "nope"}
        )


async def test_reconcile_is_a_noop_when_nothing_orphans(svc: StatesService) -> None:
    await _attached_reconciler(svc)
    # Widening the allowed set orphans nothing, so the edit lands with no close.
    await svc.update_attachment_declarations(_STATE.name, "reconciler", {"allowed": ["a", "c", "d"]})
    view = await svc.read(_STATE.name, _subject())
    assert view is not None
    assert view.data["ledger"] == [{"id": "a"}, {"id": "c"}]


# --------------------------------------------------------------------------- #
# loud-error branches (never a silent degrade)                                  #
# --------------------------------------------------------------------------- #
async def test_program_evaluation_failure_is_loud(svc: StatesService) -> None:
    template = StateTemplateDocument.model_validate(
        {
            "name": "boom",
            "schema": {"type": "object", "properties": {"ledger": {"type": "array"}}},
            "template_jq": {"bad": {"purpose": "input", "jq": '.ledger | error("kaboom")'}},
        }
    )
    await svc.put_declaration(_STATE)
    await svc.put_template(template, replace=False)
    await svc.attach(_STATE.name, "boom", AttachBody(path=[]))
    await svc.replace(_STATE.name, _subject(), {"ledger": []}, origin=_ORIGIN)
    with pytest.raises(ValueValidationError, match="failed to evaluate"):
        await svc.eval_template_jq(_STATE.name, _subject(), "bad", {})


async def test_reconcile_unknown_directive_is_loud(svc: StatesService) -> None:
    await _attached_reconciler(svc)
    with pytest.raises(TemplateValidationError, match="unknown reconcile directive"):
        await svc.update_attachment_declarations(
            _STATE.name, "reconciler", {"allowed": ["a"]}, options={"orphans": "drop"}
        )


async def test_reconcile_orphans_returning_a_non_list_is_loud(svc: StatesService) -> None:
    template = StateTemplateDocument.model_validate(
        {
            "name": "reconciler",
            "schema": {"type": "object", "properties": {"ledger": {"type": "array"}}},
            "declarations": {"schema": {"type": "object", "properties": {"allowed": {"type": "array"}}}},
            "reconcile": {"orphans": '"not a list"', "resolutions": "[]", "close": "[]"},
        }
    )
    await svc.put_declaration(_STATE)
    await svc.put_template(template, replace=False)
    await svc.attach(_STATE.name, "reconciler", AttachBody(path=[], declarations={"allowed": []}))
    await svc.replace(_STATE.name, _subject(), {"ledger": []}, origin=_ORIGIN)
    with pytest.raises(TemplateValidationError, match="reconcile orphans must return a list"):
        await svc.update_attachment_declarations(_STATE.name, "reconciler", {"allowed": ["a"]})
