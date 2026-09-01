"""The runs-index router: the list door's query parsing (filters, paging, time range)
and its 400s, and the prune door's store-off / retention-disabled / active paths.

Handlers are driven directly as coroutines with real ``Request`` objects carrying a
query string (the observability-router test idiom). The store is a spy recording the
filter/page it is handed; ``component_store_configured`` / ``run_index_settings`` are
stubbed so the doors run fully offline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

import tai42_skeleton.operations.runs as ops
from tai42_skeleton.routers import runs as router
from tai42_skeleton.runs.models import RunIndexFilter, RunOutcome, RunRow


def _req(query: str = "", method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/runs",
        "headers": [],
        "query_string": query.encode(),
        "path_params": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _q(**params) -> str:
    return urlencode(params)


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


class _SpyStore:
    def __init__(self, rows: list[RunRow] | None = None) -> None:
        self.rows = rows or []
        self.list_calls: list[dict] = []
        self.pruned_cutoff: datetime | None = None

    async def list(self, filter: RunIndexFilter, *, page: int, page_size: int) -> list[RunRow]:
        self.list_calls.append({"filter": filter, "page": page, "page_size": page_size})
        return self.rows

    async def prune(self, cutoff: datetime) -> int:
        self.pruned_cutoff = cutoff
        return 4


def _row(run_id: str, outcome: RunOutcome = "success", interaction_id: str | None = None) -> RunRow:
    return RunRow(
        run_id=run_id,
        preset_name="wx",
        preset_version=2,
        trace_id="trace-x",
        user_id="person-1",
        session_id="thread-9",
        interaction_id=interaction_id,
        outcome=outcome,
        started_at="2026-01-01T12:00:00+00:00",
        ended_at="2026-01-01T12:00:02+00:00",
    )


@pytest.fixture
def store(monkeypatch) -> _SpyStore:
    spy = _SpyStore()
    monkeypatch.setattr(ops, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(ops, "get_run_index_store", lambda: spy)
    monkeypatch.setattr(ops, "run_index_settings", lambda: SimpleNamespace(retention_days=None, max_page_size=200))
    return spy


# -- list door ---------------------------------------------------------------


async def test_list_returns_rows_and_deep_link_trace_id(store):
    store.rows = [_row("r1", outcome="parked", interaction_id="i-1"), _row("r2")]
    resp = await router.list_runs(_req(""))
    assert resp.status_code == 200
    data = _json(resp)["data"]
    assert [i["runId"] for i in data["items"]] == ["r1", "r2"]
    assert data["items"][0]["traceId"] == "trace-x"
    assert data["items"][0]["outcome"] == "parked"
    # The lifecycle-correlation key rides the wire view (camelCase, None when absent).
    assert data["items"][0]["interactionId"] == "i-1"
    assert data["items"][1]["interactionId"] is None


async def test_list_passes_every_filter_through(store):
    await router.list_runs(
        _req(_q(preset="wx", version="3", user="person-1", session="thread-9", interaction="i-9", outcome="aborted"))
    )
    f = store.list_calls[0]["filter"]
    assert (f.preset, f.version, f.user, f.session, f.interaction, f.outcome) == (
        "wx",
        3,
        "person-1",
        "thread-9",
        "i-9",
        "aborted",
    )


async def test_list_parses_time_range(store):
    await router.list_runs(_req(_q(**{"from": "2026-01-01T00:00:00+00:00", "to": "2026-01-02T00:00:00+00:00"})))
    f = store.list_calls[0]["filter"]
    assert f.t0 == datetime(2026, 1, 1, tzinfo=UTC)
    assert f.t1 == datetime(2026, 1, 2, tzinfo=UTC)


async def test_list_paging_and_next_page(store):
    store.rows = [_row(f"r{i}") for i in range(2)]
    resp = await router.list_runs(_req(_q(page="1", pageSize="2")))
    data = _json(resp)["data"]
    assert store.list_calls[0]["page"] == 1
    assert store.list_calls[0]["page_size"] == 2
    assert data["nextPage"] == 2  # a full page implies a possible next


async def test_list_page_size_is_clamped_to_cap(store, monkeypatch):
    monkeypatch.setattr(ops, "run_index_settings", lambda: SimpleNamespace(retention_days=None, max_page_size=50))
    await router.list_runs(_req(_q(pageSize="9999")))
    assert store.list_calls[0]["page_size"] == 50


async def test_list_bad_version_is_400(store):
    resp = await router.list_runs(_req(_q(version="notanint")))
    assert resp.status_code == 400
    assert "version" in _json(resp)["error"]


async def test_list_bad_outcome_is_400(store):
    resp = await router.list_runs(_req(_q(outcome="bogus")))
    assert resp.status_code == 400
    assert "outcome" in _json(resp)["error"]


async def test_list_bad_time_is_400(store):
    resp = await router.list_runs(_req(_q(**{"from": "not-a-date"})))
    assert resp.status_code == 400
    assert "from" in _json(resp)["error"]


async def test_list_store_off_is_empty_page(monkeypatch):
    monkeypatch.setattr(ops, "component_store_configured", lambda _c: False)
    resp = await router.list_runs(_req(""))
    data = _json(resp)["data"]
    assert data == {"items": [], "page": 1, "nextPage": None}


# -- prune door --------------------------------------------------------------


async def test_prune_store_off_skips(monkeypatch):
    monkeypatch.setattr(ops, "component_store_configured", lambda _c: False)
    resp = await router.prune_runs(_req(method="POST"))
    data = _json(resp)["data"]
    assert data["pruned_count"] == 0
    assert "not configured" in data["skipped"]


async def test_prune_retention_disabled_skips(store):
    # store fixture leaves retention_days=None (unset) — runs are kept forever.
    resp = await router.prune_runs(_req(method="POST"))
    data = _json(resp)["data"]
    assert data["pruned_count"] == 0
    assert "retention disabled" in data["skipped"]
    assert store.pruned_cutoff is None


async def test_prune_active_deletes_and_reports(store, monkeypatch):
    monkeypatch.setattr(ops, "run_index_settings", lambda: SimpleNamespace(retention_days=30, max_page_size=200))
    resp = await router.prune_runs(_req(method="POST"))
    data = _json(resp)["data"]
    assert data["retention_days"] == 30
    assert data["pruned_count"] == 4
    assert store.pruned_cutoff is not None
    assert store.pruned_cutoff < datetime.now(UTC)
