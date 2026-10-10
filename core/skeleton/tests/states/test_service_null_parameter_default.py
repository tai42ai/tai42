"""A template parameter whose default is an explicit ``null`` through every template write door.

The contract tells an absent default from an explicit ``null`` one (``has_default``); every door that
re-reads a template document keeps that distinction: the save, the render preview, the seed applier
and the reconcile a declarations edit runs. Driven against the in-memory ``FakeStatesStore``.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.models import AttachBody, StateTemplateDocument

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _STATE, FakeStatesStore, _FakeApp, _template_doc

# ``nullable`` defaults to an explicit ``null`` and appears nowhere in the fragment; ``label``
# defaults to ``null`` and fills the fragment's ``default``; ``cap`` has no default at all, so it
# is a marker the attachment fills.
_PARAMETERS: dict[str, Any] = {
    "nullable": {"schema": {"type": ["object", "null"]}, "default": None},
    "label": {"schema": {"type": ["string", "null"]}, "default": None},
    "cap": {"schema": {"type": "object"}},
}
_FRAGMENT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": ["string", "null"], "default": {"$parameter": "label"}},
        "count": {"$parameter": "cap"},
    },
}


def _null_default_doc(name: str = "nulls", **over: Any) -> StateTemplateDocument:
    return _template_doc(name, parameters=_PARAMETERS, schema=_FRAGMENT, **over)


def _assert_parameters_kept(parameters: dict[str, Any]) -> None:
    assert parameters["nullable"] == {"schema": {"type": ["object", "null"]}, "default": None}
    assert parameters["label"] == {"schema": {"type": ["string", "null"]}, "default": None}
    assert parameters["cap"] == {"schema": {"type": "object"}}


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def test_put_template_keeps_an_explicit_null_default_apart_from_no_default(svc: StatesService) -> None:
    saved = await svc.put_template(_null_default_doc(), replace=False)
    _assert_parameters_kept(saved.model_dump()["parameters"])
    assert saved.parameters["nullable"].has_default
    assert not saved.parameters["cap"].has_default
    got = await svc.get_template("nulls")
    assert got is not None
    _assert_parameters_kept(got.model_dump()["parameters"])


async def test_render_template_accepts_an_explicit_null_default(svc: StatesService) -> None:
    rendered = await svc.render_template(_null_default_doc())
    _assert_parameters_kept(rendered.model_dump()["parameters"])


async def test_template_seed_with_an_explicit_null_default_is_stored(svc: StatesService) -> None:
    svc.register_template_seed(_null_default_doc("seeded-nulls"))
    await svc.apply_template_seeds()
    got = await svc.get_template("seeded-nulls")
    assert got is not None
    _assert_parameters_kept(got.model_dump()["parameters"])


async def test_attach_renders_the_null_default_into_the_effective_schema(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(_null_default_doc(), replace=False)
    await svc.attach("alerts", "nulls", AttachBody(path=["sub"], parameters={"cap": {"type": "integer"}}))
    effective = await svc.effective_schema_for("alerts")
    sub = effective["properties"]["sub"]
    assert sub["properties"]["label"] == {"type": ["string", "null"], "default": None}
    assert sub["properties"]["count"] == {"type": "integer"}


async def test_declarations_edit_reconciles_a_template_with_an_explicit_null_default(svc: StatesService) -> None:
    doc = _null_default_doc(
        declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
        reconcile={
            "orphans": {"content": "[]"},
            "close": {"content": "."},
            "resolutions": {"content": "[]"},
        },
    )
    await svc.put_declaration(_STATE)
    await svc.put_template(doc, replace=False)
    await svc.attach(
        "alerts", "nulls", AttachBody(path=["sub"], parameters={"cap": {"type": "integer"}}, declarations={"n": 1})
    )
    await svc.update_attachment_declarations("alerts", "nulls", {"n": 2})
    (row,) = await svc.list_attachments("alerts", template="nulls")
    assert row["declarations"] == {"n": 2}
