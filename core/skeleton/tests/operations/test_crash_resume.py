"""The crash-resume seam: persisted arguments + the generic ``crash_resume`` flag on the
record, and the re-dispatch branch in the liveness→``lost`` reconciler.

A run whose record carries the flag is re-dispatched from scratch when its supervisor
dies; every un-flagged run keeps today's quiet ``lost`` behavior byte-for-byte.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

import tai42_skeleton.operations.tool_runs as ops
from tai42_skeleton.operations.tool_runs import ToolRunStore, _reconcile_lost_with_liveness
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

from .._fakes.tool_runs_redis import FakeRedis


@pytest.fixture
def wired(monkeypatch):
    fake = FakeRedis()
    settings = ToolRunsSettings()

    @asynccontextmanager
    async def ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake

    monkeypatch.setattr(ops, "client_ctx", ctx)
    monkeypatch.setattr(ops, "tool_runs_settings", lambda: settings)
    monkeypatch.setattr(ops, "_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    store = ToolRunStore(settings.key_prefix)
    yield store, fake, settings
    for task in list(ops._SUPERVISORS):
        task.cancel()


async def test_create_run_persists_arguments_and_flag_when_crash_resume(wired) -> None:
    store, fake, settings = wired
    await store.create_run(
        fake,
        "r1",
        "alpha",
        "2026-01-01T00:00:00",
        1.0,
        settings,
        user_id="u1",
        arguments={"x": 1},
        crash_resume=True,
    )
    record = await store.get_run(fake, "r1")
    assert record is not None
    assert record["crash_resume"] == "1"
    assert json.loads(record["arguments"]) == {"x": 1}


async def test_create_run_stores_neither_for_an_unflagged_run(wired) -> None:
    store, fake, settings = wired
    await store.create_run(fake, "r2", "alpha", "2026-01-01T00:00:00", 1.0, settings, user_id="u1")
    record = await store.get_run(fake, "r2")
    assert record is not None
    assert "crash_resume" not in record
    assert "arguments" not in record


async def test_reconcile_redispatches_a_flagged_lost_run(wired, monkeypatch) -> None:
    store, fake, settings = wired
    spawned: list[tuple[str, dict]] = []
    monkeypatch.setattr(ops, "_spawn_crash_resume", lambda run_id, record: spawned.append((run_id, record)))
    await store.create_run(
        fake, "r3", "alpha", "2026-01-01T00:00:00", 1.0, settings, user_id="u1", arguments={"x": 1}, crash_resume=True
    )
    record = await store.get_run(fake, "r3")
    assert record is not None
    # Liveness expired (dead supervisor): the reconciler marks it lost AND re-dispatches.
    result = await _reconcile_lost_with_liveness(fake, store, "r3", record, liveness_present=False, ttl=60)
    assert result["status"] == "lost"
    assert len(spawned) == 1
    assert spawned[0][0] == "r3"


async def test_reconcile_does_not_redispatch_an_unflagged_lost_run(wired, monkeypatch) -> None:
    store, fake, settings = wired
    spawned: list = []
    monkeypatch.setattr(ops, "_spawn_crash_resume", lambda run_id, record: spawned.append(run_id))
    await store.create_run(fake, "r4", "alpha", "2026-01-01T00:00:00", 1.0, settings, user_id="u1")
    record = await store.get_run(fake, "r4")
    assert record is not None
    result = await _reconcile_lost_with_liveness(fake, store, "r4", record, liveness_present=False, ttl=60)
    # Today's quiet lost EXACTLY — no re-dispatch.
    assert result["status"] == "lost"
    assert spawned == []


async def test_reconcile_leaves_a_live_run_untouched(wired, monkeypatch) -> None:
    store, fake, settings = wired
    spawned: list = []
    monkeypatch.setattr(ops, "_spawn_crash_resume", lambda run_id, record: spawned.append(run_id))
    await store.create_run(
        fake, "r5", "alpha", "2026-01-01T00:00:00", 1.0, settings, user_id="u1", arguments={}, crash_resume=True
    )
    record = await store.get_run(fake, "r5")
    assert record is not None
    # Liveness present → never reconciled, never re-dispatched.
    result = await _reconcile_lost_with_liveness(fake, store, "r5", record, liveness_present=True, ttl=60)
    assert result["status"] == "running"
    assert spawned == []
