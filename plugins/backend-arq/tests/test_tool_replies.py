"""The arq backend's ``backend_*`` tools as the registry serves them: arguments and replies."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from arq.constants import abort_jobs_ss, in_progress_key_prefix
from tai42_kit.backend.tool_surface import UnsupportedBackendTool

from tai42_backend_arq.settings import arq_settings


def _seed(redis: Any, name: str = "s1", *, enabled: bool = True) -> str:
    key = f"arq:schedule:{name}"
    redis._store[key] = {
        b"target": b"tool_execution",
        b"args": orjson.dumps([]),
        b"kwargs": orjson.dumps({arq_settings().tool_name_arg: "t"}),
        b"schedule": orjson.dumps({"kind": "interval", "every": 60.0, "relative": False}),
        b"cron_or_interval": b"60",
        b"enabled": b"true" if enabled else b"false",
        b"last_scheduled_ts": b"0.0",
    }
    return key


@pytest.mark.parametrize(
    ("tool", "status", "enabled"),
    [
        ("backend_enable_schedule", "enabled", False),
        ("backend_disable_schedule", "disabled", True),
        ("backend_run_schedule_now", "queued", True),
    ],
)
async def test_a_status_reply_carries_the_name(stub_app, fake_redis, bind_pool, tool, status, enabled) -> None:
    bind_pool(fake_redis)
    _seed(fake_redis, enabled=enabled)
    assert await stub_app.tools.registered[tool]("s1") == {"status": status, "name": "s1"}
    assert await stub_app.tools.registered[tool]("nope") == {"status": "not_found", "name": "nope"}


async def test_update_without_a_change_replies_the_one_skipped_text(stub_app, fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed(fake_redis)
    assert await stub_app.tools.registered["backend_update_schedule"]("s1") == {
        "status": "skipped",
        "name": "s1",
        "message": "No changes provided; nothing updated.",
    }


async def test_get_replies_the_schedule_detail_with_a_zero_next_run(stub_app, fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed(fake_redis)
    assert await stub_app.tools.registered["backend_get_schedule"]("s1") == {
        "name": "s1",
        "tool": "t",
        "args": [],
        "kwargs": {},
        "cadence": {"kind": "interval", "every": 60.0, "relative": False},
        "enabled": True,
        "next_run_at_ts": 0.0,
        "next_run_at_iso": "1970-01-01T00:00:00+00:00",
        "error": None,
    }


@pytest.mark.parametrize(
    "tool",
    [
        "backend_ping_worker",
        "backend_active_tasks",
        "backend_registered_tasks",
        "backend_worker_stats",
        "backend_worker_queues",
    ],
)
async def test_a_worker_name_is_refused(stub_app, tool) -> None:
    with pytest.raises(UnsupportedBackendTool, match=f"backend 'arq' does not support {tool} with a worker_name"):
        await stub_app.tools.registered[tool](worker_name="w1")


async def test_cancel_without_terminate_refuses_a_running_task(stub_app, arq_redis) -> None:
    await arq_redis.set(in_progress_key_prefix + "job-1", b"1")
    refusal = "^backend 'arq' does not support backend_cancel_task without terminate for a running task$"
    with pytest.raises(UnsupportedBackendTool, match=refusal):
        await stub_app.tools.registered["backend_cancel_task"]("job-1")
    assert await arq_redis.zscore(abort_jobs_ss, "job-1") is None
