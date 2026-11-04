"""The RQ backend's ``backend_*`` tools as the registry serves them: arguments and replies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from rq import Queue
from tai42_kit.backend.tool_surface import UnsupportedBackendTool

from tai42_backend_rq import schedule_store as store_module
from tai42_backend_rq.settings import rq_settings

from .conftest import (
    FakeSyncRedis,
    _make_stateful_scheduler,
    _StatefulScheduledJob,
    make_client_ctx,
    scheduler_factory,
    stub_app,
)


def _tool(name: str) -> Any:
    return stub_app.tools.registered[name]


def _job(name: str = "sched-1") -> _StatefulScheduledJob:
    return _StatefulScheduledJob(name, "tool_execution", [], {rq_settings().tool_name_arg: "echo"}, {"interval": 60})


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, float]]:
    """One stored schedule ``sched-1`` behind a stateful fake scheduler and its zset."""
    jobs: dict[str, Any] = {"sched-1": _job()}
    zset: dict[str, float] = {"sched-1": 1.0}
    monkeypatch.setattr(store_module, "scheduler_for", scheduler_factory(_make_stateful_scheduler(jobs, zset)))
    redis = FakeSyncRedis(zsets={rq_settings().rq_scheduler_zset: zset})
    monkeypatch.setattr(store_module, "client_ctx", make_client_ctx(redis))
    return jobs, zset


async def test_enable_of_a_stored_schedule_replies_enabled_without_a_write(store):
    jobs, zset = store
    before = (dict(jobs), dict(zset))
    assert await _tool("backend_enable_schedule")("sched-1") == {"status": "enabled", "name": "sched-1"}
    assert (jobs, zset) == before


async def test_disable_is_refused_and_keeps_the_schedule(store):
    jobs, _ = store
    with pytest.raises(NotImplementedError, match="backend 'rq' does not support backend_disable_schedule"):
        await _tool("backend_disable_schedule")("sched-1")
    assert "sched-1" in jobs


async def test_update_without_a_change_replies_the_one_skipped_text(store):
    assert await _tool("backend_update_schedule")("sched-1") == {
        "status": "skipped",
        "name": "sched-1",
        "message": "No changes provided; nothing updated.",
    }


async def test_update_next_run_in_ms_wins_over_next_run_at_ts(store):
    _, zset = store
    at_ts = datetime(2031, 5, 1, tzinfo=UTC).timestamp()
    before = datetime.now(UTC).timestamp()
    out = await _tool("backend_update_schedule")("sched-1", next_run_in_ms=60_000, next_run_at_ts=at_ts)
    assert before + 60 <= zset["sched-1"] < at_ts
    assert out["next_run_at_ts"] == zset["sched-1"]


async def test_update_reply_names_the_previous_and_new_cadence(store):
    out = await _tool("backend_update_schedule")("sched-1", new_schedule=300)
    assert out["status"] == "updated"
    assert out["previous_schedule"] == {"kind": "interval", "every": 60.0, "relative": False}
    assert out["new_schedule"] == {"kind": "interval", "every": 300.0, "relative": False}


async def test_cancel_without_terminate_leaves_a_running_task_to_finish(rq_server):
    job = Queue(rq_settings().queue_name, connection=rq_server).enqueue("os.getpid")
    with rq_server.pipeline() as pipe:
        job.prepare_for_execution("w1", pipeline=pipe)
        pipe.execute()
    assert await _tool("backend_cancel_task")(job.id) == f"Task {job.id} is running and is left to finish"


async def test_registered_tasks_takes_a_worker_name():
    with pytest.raises(UnsupportedBackendTool, match="backend 'rq' does not support backend_registered_tasks"):
        await _tool("backend_registered_tasks")(worker_name="w1")


async def test_import_of_a_disabled_record_names_the_one_text(store):
    record = {"name": "off", "tool": "echo", "cadence": {"kind": "interval", "every": 60}, "enabled": False}
    report = await _tool("backend_import_schedules")([record])
    assert report["skipped"] == 1
    assert report["errors"] == [
        {
            "index": 0,
            "name": "off",
            "error": (
                "schedule 'off' has enabled=False; the rq backend has no disabled-schedule representation, "
                "so it was not created as an active schedule"
            ),
        }
    ]
