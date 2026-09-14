"""The states service's record doors (replace / merge / apply / erase / fold) and the
listing / search / prune reads — driven against the in-memory ``FakeStatesStore``."""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import (
    InvalidPathError,
    StateNotFoundError,
    SubjectFoldError,
    ValueValidationError,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _ORIGIN, _STATE, FakeStatesStore, _FakeApp, _subject


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    # The attach-check evaluation and the by-id save compile render through the bound
    # resource manager, so bind a fake for the service's lifetime.
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


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
