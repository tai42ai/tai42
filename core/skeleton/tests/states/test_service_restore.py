"""The states service's ``restore_records`` door: every subject is validated before any
write, so a bad row aborts the batch and nothing lands — against the in-memory fake store."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.states.errors import SubjectRefusedError
from tai42_contract.states.models import StateContext, StateDeclaration, SubjectCandidates, WriteOrigin

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService, state_context

from .fake_service_store import FakeStatesStore


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


_PERSON_STATE = StateDeclaration(
    name="alerts",
    schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    subject_kinds=["person"],
    default_subject_kind="person",
)


def _patch_person_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``known`` resolves, to target ``agent/a`` — every other id is unknown."""

    class _FakePersonStore:
        def __init__(self, settings: object) -> None:
            self._settings = settings

        async def get_by_id(self, person_id: str) -> object:
            if person_id == "known":
                return SimpleNamespace(person_id="known", target_kind="agent", target_name="a")
            return None

    import tai42_skeleton.conversations.persons as persons_mod
    import tai42_skeleton.conversations.settings as settings_mod

    monkeypatch.setattr(persons_mod, "ConversationPersonStore", _FakePersonStore)
    monkeypatch.setattr(settings_mod, "ConversationsSettings", lambda: object())


def _person_row(key: str, n: int) -> dict[str, Any]:
    return {"target_kind": "agent", "target_name": "a", "subject_kind": "person", "subject_key": key, "data": {"n": n}}


async def test_restore_records_refuses_a_bad_person_row_and_writes_nothing(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await svc.put_declaration(_PERSON_STATE)
    _patch_person_store(monkeypatch)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    rows = [_person_row("known", 1), _person_row("ghost", 2)]
    # the offending row's index AND its subject ride the refusal, and nothing is written
    with pytest.raises(SubjectRefusedError, match="restore row 1"):
        await svc.restore_records("alerts", rows, origin=WriteOrigin(consumer="backup-restore"))
    assert store.records == {}


async def test_restore_records_lands_a_clean_batch(svc: StatesService, monkeypatch: pytest.MonkeyPatch) -> None:
    await svc.put_declaration(_PERSON_STATE)
    _patch_person_store(monkeypatch)
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    ctx = StateContext(
        door="operator",
        candidates=SubjectCandidates(target_kind="agent", target_name="a"),
        actor="operator-1",
    )
    with state_context(ctx):
        await svc.restore_records("alerts", [_person_row("known", 7)], origin=WriteOrigin(consumer="backup-restore"))
    assert store.records[("alerts", "agent", "a", "person", "known")] == {"n": 7}
    assert store.applied_origins[-1].door == "operator"
