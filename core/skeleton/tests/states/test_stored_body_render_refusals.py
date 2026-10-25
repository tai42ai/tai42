"""A state template program body the template engine cannot render is a loud 422 refusal.

A real :class:`ResourceManager` over an in-memory store renders each body. A stored body (by
``id``) or an inline body with broken Jinja fails its render with a ``jinja2.TemplateError``; the
save door and the run-time render each refuse it as a ``TemplateValidationError`` naming the
program and the id (or the inline body), never an unmapped error.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import AttachBody, WriteOrigin

from tai42_skeleton.app import instance
from tai42_skeleton.operations import ValidationRejectedError
from tai42_skeleton.operations import states as ops
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager

from ..template.test_resource_manager import _InMemoryStorage
from .fake_service_store import _STATE, FakeStatesStore, _subject

_ORIGIN = WriteOrigin(consumer="c")
_BROKEN = "{{ oops"
_SCHEMA = {"type": "object", "properties": {"ledger": {"type": "array", "items": {"type": "object"}}}}

# The stored objects the in-memory store serves; reset per test.
_STORED_BASE = {
    "stored-any-due.jq": "(.ledger // []) | length > 0",
    "stored-broken.jq": _BROKEN,
    "stored-orphans.jq": "[]",
}
_STORED: dict[str, str] = {}


class _Store(_InMemoryStorage):
    """Serves ``_STORED`` itself, so a test edits a stored body in place."""

    def __init__(self) -> None:
        super().__init__()
        self.items = _STORED


class _App:
    def __init__(self, manager: ResourceManager) -> None:
        self.storage = type("_S", (), {"resource_manager": manager})()


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ResourceManager:
    _STORED.clear()
    _STORED.update(_STORED_BASE)
    registry = StorageRegistry()
    registry.register_storage(_Store)
    monkeypatch.setattr(instance.app._storage_registry, "_provider", registry.provider)
    return ResourceManager(registry.provider, on_evicted=lambda _eviction: None)


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch, manager: ResourceManager):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    service = StatesService(store=FakeStatesStore())  # type: ignore[arg-type]
    monkeypatch.setattr(ops, "_states", lambda: service)
    with tai42_app.bound(_App(manager)):  # type: ignore[arg-type]
        yield service


async def _put(name: str, body: dict[str, Any]) -> None:
    await ops.put_state_template(name, {"schema": _SCHEMA, **body})


async def test_a_stored_program_body_with_broken_jinja_is_refused_at_save(svc: StatesService) -> None:
    with pytest.raises(ValidationRejectedError) as excinfo:
        await _put("t", {"template_jq": {"any_due": {"purpose": "input", "jq": {"id": "stored-broken.jq"}}}})
    assert excinfo.value.status == 422
    assert (
        "template 't' template_jq 'any_due' references stored id 'stored-broken.jq', which could not be rendered"
        in str(excinfo.value)
    )


async def test_a_stored_declarations_check_with_broken_jinja_is_refused_at_save(svc: StatesService) -> None:
    body = {"declarations": {"schema": {"type": "object"}, "check": {"id": "stored-broken.jq"}}}
    with pytest.raises(ValidationRejectedError) as excinfo:
        await _put("t", body)
    assert excinfo.value.status == 422
    assert (
        "template 't' declarations check references stored id 'stored-broken.jq', which could not be rendered"
        in str(excinfo.value)
    )


async def test_a_stored_reconcile_program_with_broken_jinja_is_refused_at_save(svc: StatesService) -> None:
    body = {
        "declarations": {"schema": {"type": "object"}},
        "reconcile": {
            "orphans": {"id": "stored-broken.jq"},
            "resolutions": {"content": "[]"},
            "close": {"content": "[]"},
        },
    }
    with pytest.raises(ValidationRejectedError) as excinfo:
        await _put("t", body)
    assert excinfo.value.status == 422
    assert "template 't' reconcile orphans references stored id 'stored-broken.jq', which could not be rendered" in str(
        excinfo.value
    )


async def test_a_stored_program_body_broken_after_save_is_refused_at_run_time(
    svc: StatesService, manager: ResourceManager
) -> None:
    await svc.put_declaration(_STATE)
    await _put("t", {"template_jq": {"any_due": {"purpose": "input", "jq": {"id": "stored-any-due.jq"}}}})
    await svc.attach(_STATE.name, "t", AttachBody(path=[]))
    await svc.replace(_STATE.name, _subject(), {"ledger": [{"id": "a"}]}, origin=_ORIGIN)
    _STORED["stored-any-due.jq"] = _BROKEN
    manager.clear_cache()
    with pytest.raises(
        TemplateValidationError,
        match=r"template 't' template_jq 'any_due' references stored id 'stored-any-due\.jq', "
        r"which could not be rendered",
    ):
        await svc.eval_template_jq(_STATE.name, _subject(), "any_due", {})


async def test_an_inline_program_body_with_broken_jinja_is_refused_at_run_time(svc: StatesService) -> None:
    # A jq string literal holding ``{{`` compiles as jq at save, then fails its Jinja render when
    # the program runs.
    await svc.put_declaration(_STATE)
    await _put("t", {"template_jq": {"any_due": {"purpose": "input", "jq": {"content": '"{{ oops"'}}}})
    await svc.attach(_STATE.name, "t", AttachBody(path=[]))
    await svc.replace(_STATE.name, _subject(), {"ledger": []}, origin=_ORIGIN)
    with pytest.raises(
        TemplateValidationError, match=r"template 't' template_jq 'any_due' is not a renderable template"
    ):
        await svc.eval_template_jq(_STATE.name, _subject(), "any_due", {})


async def test_a_stored_reconcile_program_broken_after_save_is_refused_at_run_time(
    svc: StatesService, manager: ResourceManager
) -> None:
    body = {
        "declarations": {"schema": {"type": "object", "properties": {"allowed": {"type": "array"}}}},
        "reconcile": {
            "orphans": {"id": "stored-orphans.jq"},
            "resolutions": {"content": '["closed"]'},
            "close": {"content": "[]"},
        },
    }
    await svc.put_declaration(_STATE)
    await _put("t", body)
    await svc.attach(_STATE.name, "t", AttachBody(path=[], declarations={"allowed": ["a"]}))
    await svc.replace(_STATE.name, _subject(), {"ledger": [{"id": "a"}]}, origin=_ORIGIN)
    _STORED["stored-orphans.jq"] = _BROKEN
    manager.clear_cache()
    with pytest.raises(
        TemplateValidationError,
        match=r"template 't' reconcile orphans references stored id 'stored-orphans\.jq', which could not be rendered",
    ):
        await svc.update_attachment_declarations(
            _STATE.name, "t", {"allowed": []}, options={"orphans": "close", "resolution": "closed"}
        )
