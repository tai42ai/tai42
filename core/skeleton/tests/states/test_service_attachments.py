"""The states service's attachment doors (list / attach / update-declarations / detach), the
declarations-check evaluation over effective parameters, and the attach-value validators —
driven against the in-memory ``FakeStatesStore`` with a fake resource manager bound."""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import (
    AttachConflictError,
    StateNotFoundError,
    TemplateValidationError,
)
from tai42_contract.states.models import AttachBody

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.templates import validate_template

from .fake_service_store import _STATE, FakeStatesStore, _FakeApp, _template_doc


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def test_list_attachments_every_form(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_template_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    assert len(await svc.list_attachments("alerts", template="m")) == 1
    assert await svc.list_attachments("alerts", template="absent") == []
    assert len(await svc.list_attachments("alerts")) == 1
    assert len(await svc.list_attachments(template="m")) == 1
    assert len(await svc.list_attachments()) == 1


async def test_attach_refuses_a_duplicate(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_template_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    with pytest.raises(AttachConflictError, match="already attached"):
        await svc.attach("alerts", "m", AttachBody(path=["other"]))


async def test_attach_missing_template_raises(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(StateNotFoundError, match="no template"):
        await svc.attach("alerts", "absent", AttachBody(path=["sub"]))


def _capped_template(name: str = "capped"):
    """A template whose declarations ``check`` constrains a declared ``count`` against the
    attach's effective ``limit`` parameter, read as ``$parameters.limit``."""
    return _template_doc(
        name,
        schema={"type": "object", "properties": {"box": {"type": "object"}}},
        parameters={"limit": {"schema": {"type": "integer"}, "default": 5}},
        declarations={
            "schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
            "check": {"content": 'if .count <= $parameters.limit then true else "count exceeds the attach limit" end'},
        },
    )


async def test_attach_check_reads_effective_parameters(svc: StatesService) -> None:
    """A declarations check reads the attach's supplied parameters as ``$parameters``: a
    declaration within the supplied ``limit`` attaches, one exceeding it is refused with the
    check's message."""
    await svc.put_declaration(_STATE)
    await svc.put_template(_capped_template(), replace=False)
    with pytest.raises(TemplateValidationError, match="count exceeds the attach limit"):
        await svc.attach("alerts", "capped", AttachBody(path=["a"], parameters={"limit": 8}, declarations={"count": 9}))
    await svc.attach("alerts", "capped", AttachBody(path=["a"], parameters={"limit": 8}, declarations={"count": 7}))


async def test_attach_check_sees_the_parameter_default(svc: StatesService) -> None:
    """An attach supplying no ``limit`` sees the template default (5) in the check, so the check
    constrains against the same value the runtime persists."""
    await svc.put_declaration(_STATE)
    await svc.put_template(_capped_template(), replace=False)
    with pytest.raises(TemplateValidationError, match="count exceeds the attach limit"):
        await svc.attach("alerts", "capped", AttachBody(path=["a"], declarations={"count": 6}))
    await svc.attach("alerts", "capped", AttachBody(path=["a"], declarations={"count": 4}))


def _capped_template_by_id(name: str = "capped-by-id"):
    """The capped template whose declarations ``check`` is carried by a stored id rather than
    inline (the stored resource resolves to the same jq)."""
    return _template_doc(
        name,
        schema={"type": "object", "properties": {"box": {"type": "object"}}},
        parameters={"limit": {"schema": {"type": "integer"}, "default": 5}},
        declarations={
            "schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
            "check": {"id": "capped-check"},
        },
    )


async def test_attach_check_by_id_renders_then_evaluates(svc: StatesService) -> None:
    # A by-id declarations check is compiled at the save (put_template) and rendered again at
    # attach IMMEDIATELY before it is evaluated over the declaration values.
    await svc.put_declaration(_STATE)
    await svc.put_template(_capped_template_by_id(), replace=False)
    with pytest.raises(TemplateValidationError, match="count exceeds the attach limit"):
        await svc.attach(
            "alerts", "capped-by-id", AttachBody(path=["a"], parameters={"limit": 8}, declarations={"count": 9})
        )
    await svc.attach(
        "alerts", "capped-by-id", AttachBody(path=["a"], parameters={"limit": 8}, declarations={"count": 7})
    )


async def test_put_template_rejects_unfetchable_by_id_check(svc: StatesService) -> None:
    # The save door renders the by-id check to compile it; a stored id that cannot be fetched
    # fails the save loudly, naming the field and the id.
    doc = _template_doc(
        "ghost-check",
        declarations={"schema": {"type": "object"}, "check": {"id": "missing-resource"}},
    )
    with pytest.raises(TemplateValidationError, match="references stored id 'missing-resource'"):
        await svc.put_template(doc, replace=False)


async def test_effective_schema_for_undeclared_raises(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no state declared"):
        await svc.effective_schema_for("absent")


def test_register_consumer_lister_refuses_duplicate_kind(svc: StatesService) -> None:
    async def lister(_state: str):
        return []

    svc.register_consumer_lister("hook", lister)
    with pytest.raises(ValueError, match="already registered"):
        svc.register_consumer_lister("hook", lister)


async def test_update_attach_declarations_paths(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    decl_template = _template_doc(
        "m",
        declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    )
    await svc.put_template(decl_template, replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"], declarations={"n": 1}))
    await svc.update_attachment_declarations("alerts", "m", {"n": 2})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert store.attachments[("alerts", "m")]["declarations"] == {"n": 2}
    with pytest.raises(StateNotFoundError, match="not attached"):
        await svc.update_attachment_declarations("alerts", "absent", {})


async def test_detach_missing_raises(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    with pytest.raises(StateNotFoundError, match="not attached"):
        await svc.detach("alerts", "absent")


async def test_validate_attach_values_parameter_branches(svc: StatesService) -> None:
    param_template = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"cap": {"$parameter": "cap"}}},
            "parameters": {"cap": {"schema": {"type": "integer"}}},
        }
    )
    with pytest.raises(TemplateValidationError, match="unknown parameter"):
        await svc._validate_attach_values(param_template, {"nope": 1}, {})
    with pytest.raises(TemplateValidationError, match="is invalid"):
        await svc._validate_attach_values(param_template, {"cap": "not-an-int"}, {})
    with pytest.raises(TemplateValidationError, match="must supply parameter"):
        await svc._validate_attach_values(param_template, {}, {})


async def test_validate_attach_values_declaration_branches(svc: StatesService) -> None:
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
            "declarations": {
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "check": {"content": ".n > 0"},
            },
        }
    )
    with pytest.raises(TemplateValidationError, match="invalid under template"):
        await svc._validate_attach_values(checked, {}, {"n": "bad"})
    with pytest.raises(TemplateValidationError, match="rejected by template"):
        await svc._validate_attach_values(checked, {}, {"n": -1})
    # a passing declaration set clears every gate (the happy path through the check)
    await svc._validate_attach_values(checked, {}, {"n": 3})


async def test_validate_attach_values_check_string_message_and_eval_error(svc: StatesService) -> None:
    stringy = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "declarations": {
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "check": {"content": 'if .n > 0 then true else "n must be positive" end'},
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
                "check": {"content": '.n | error("boom")'},
            },
        }
    )
    with pytest.raises(TemplateValidationError, match="failed to evaluate"):
        await svc._validate_attach_values(erroring, {}, {"n": 1})


def test_validate_attach_path_refusals(svc: StatesService) -> None:
    with pytest.raises(AttachConflictError, match="must be a list"):
        svc._validate_attach_path("not-a-list")
    with pytest.raises(AttachConflictError, match="non-empty object key"):
        svc._validate_attach_path([""])
    with pytest.raises(AttachConflictError, match="non-empty object key"):
        svc._validate_attach_path([123])
