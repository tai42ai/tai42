"""The states service's template doors (list / get / put / delete) and the by-id
(``TemplatedText``) authored schema bodies for declarations and templates — driven against
the in-memory ``FakeStatesStore`` with a fake resource manager bound."""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import (
    StateNotFoundError,
    TemplateExistsError,
    TemplateInUseError,
    TemplateValidationError,
)
from tai42_contract.states.models import AttachBody, StateDeclaration, StateTemplateDocument
from tai42_contract.template import TemplatedText
from tai42_kit.utils.render import SchemaBodyError

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _STATE, FakeStatesStore, _FakeApp, _template_doc


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def test_list_and_get_template(svc: StatesService) -> None:
    await svc.put_template(_template_doc("m1"), replace=False)
    listed = await svc.list_templates()
    assert [m.name for m in listed] == ["m1"]
    got = await svc.get_template("m1")
    assert got is not None
    assert got.name == "m1"
    assert await svc.get_template("absent") is None


async def test_put_template_without_replace_refuses_existing(svc: StatesService) -> None:
    await svc.put_template(_template_doc("m"), replace=False)
    with pytest.raises(TemplateExistsError, match="already exists"):
        await svc.put_template(_template_doc("m"), replace=False)


async def test_put_template_replace_revalidates_live_attachments(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_template_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    # a replace with a still-compatible body backfills the attach's parameters and succeeds
    await svc.put_template(
        _template_doc(
            "m", schema={"type": "object", "properties": {"y": {"type": "integer"}, "z": {"type": "string"}}}
        ),
        replace=True,
    )
    got = await svc.get_template("m")
    assert got is not None
    assert isinstance(got.schema_, dict)
    assert "z" in got.schema_["properties"]


async def test_put_template_replace_refused_when_a_validator_now_rejects(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_template_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))

    async def refusing(doc, declarations, effective) -> None:
        raise TemplateValidationError("no longer valid on this attach")

    svc.register_attach_validator(refusing)
    with pytest.raises(TemplateInUseError, match="no longer validates"):
        await svc.put_template(_template_doc("m"), replace=True)


async def test_delete_template_paths(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no template"):
        await svc.delete_template("absent")
    await svc.put_declaration(_STATE)
    await svc.put_template(_template_doc("m"), replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"]))
    with pytest.raises(TemplateInUseError, match="attached on state"):
        await svc.delete_template("m")
    await svc.detach("alerts", "m")
    await svc.delete_template("m")
    assert await svc.get_template("m") is None


async def test_put_declaration_resolves_a_by_id_base_schema(svc: StatesService) -> None:
    """A declaration whose base ``schema`` is named by stored id resolves to that schema for
    validation + effective composition, while the stored/served base keeps the by-id union."""
    decl = StateDeclaration(
        name="byid",
        schema=TemplatedText(id="stored-state-schema"),
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )
    await svc.put_declaration(decl)
    served = await svc.served_declaration("byid")
    # The stored/served base is the union (the by-id reference), not the resolved schema.
    assert served["schema"] == {"id": "stored-state-schema"}
    # The effective schema (no attachments) is the RESOLVED base schema.
    assert served["effective_schema"] == {"type": "object", "properties": {"n": {"type": "integer"}}}


async def test_put_declaration_by_id_unfetchable_fails_loudly(svc: StatesService) -> None:
    decl = StateDeclaration(
        name="byid",
        schema=TemplatedText(id="missing-schema"),
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )
    with pytest.raises(SchemaBodyError, match="could not be rendered"):
        await svc.put_declaration(decl)


async def test_put_declaration_by_id_invalid_json_fails_loudly(svc: StatesService) -> None:
    decl = StateDeclaration(
        name="byid",
        schema=TemplatedText(id="not-json-schema"),
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )
    with pytest.raises(SchemaBodyError, match="did not render to valid JSON"):
        await svc.put_declaration(decl)


async def test_put_template_resolves_a_by_id_fragment_schema(svc: StatesService) -> None:
    """A template whose fragment ``schema`` is named by stored id resolves to that fragment for
    structural validation, while the stored/served body keeps the by-id union."""
    doc = StateTemplateDocument.model_validate(
        {"kind": "state-template", "name": "byidtpl", "schema": {"id": "stored-fragment-schema"}}
    )
    await svc.put_template(doc, replace=True)
    got = await svc.get_template("byidtpl")
    assert got is not None
    assert got.schema_ == TemplatedText(id="stored-fragment-schema")


async def test_put_template_by_id_unfetchable_fails_loudly(svc: StatesService) -> None:
    doc = StateTemplateDocument.model_validate(
        {"kind": "state-template", "name": "byidtpl", "schema": {"id": "missing-fragment"}}
    )
    with pytest.raises(SchemaBodyError, match="could not be rendered"):
        await svc.put_template(doc, replace=True)


async def test_put_template_by_id_invalid_json_fails_loudly(svc: StatesService) -> None:
    doc = StateTemplateDocument.model_validate(
        {"kind": "state-template", "name": "byidtpl", "schema": {"id": "not-json-schema"}}
    )
    with pytest.raises(SchemaBodyError, match="did not render to valid JSON"):
        await svc.put_template(doc, replace=True)
