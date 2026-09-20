"""The states service's backup-restore doors — subject validation on ``restore_records`` and
the ``restore_aliases`` delegation — against the in-memory ``FakeStatesStore``."""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import SubjectRefusedError

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _ORIGIN, _STATE, FakeStatesStore, _FakeApp


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


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
