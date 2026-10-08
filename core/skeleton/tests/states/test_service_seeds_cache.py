"""The states service's template-seed applier and the
declaration stats/delete helpers — driven against the in-memory ``FakeStatesStore``."""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import StateNotFoundError

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _STATE, FakeStatesStore, _FakeApp, _template_doc


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def test_register_and_apply_template_seeds(svc: StatesService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    svc.register_template_seed(_template_doc("seeded"))
    await svc.apply_template_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "seeded" in store.templates


async def test_apply_template_seeds_is_a_noop_when_feature_off(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.register_template_seed(_template_doc("seeded"))
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: False)
    await svc.apply_template_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "seeded" not in store.templates


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


async def test_an_applied_seed_reads_back_through_get_template(svc: StatesService) -> None:
    svc.register_template_seed(_template_doc("seeded"))
    await svc.apply_template_seeds()
    served = await svc.get_template("seeded")
    assert served is not None
    assert served.name == "seeded"


async def test_a_seed_that_does_not_validate_is_refused_naming_it(svc: StatesService) -> None:
    from tai42_contract.states.errors import TemplateValidationError

    bad = _template_doc("broken", schema={"type": "object", "properties": {"y": {"$parameter": "missing"}}})
    svc.register_template_seed(bad)
    with pytest.raises(TemplateValidationError, match="broken"):
        await svc.apply_template_seeds()
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "broken" not in store.templates
