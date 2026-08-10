"""The Celery application factory wires RedBeat, pool restarts, and task events."""

from __future__ import annotations

from typing import Any

import tai42_backend_celery.core.app as app_module
from tai42_backend_celery.core.settings import celery_settings


def test_create_celery_app_wires_redbeat_and_events() -> None:
    conf: Any = app_module.celery_app.conf
    assert conf["beat_scheduler"] == "redbeat.RedBeatScheduler"
    assert conf["redbeat_key_prefix"] == "redbeat:"
    assert conf["timezone"] == "UTC"
    assert conf["worker_pool_restarts"] is True
    assert conf["worker_send_task_events"] is True
    assert conf["task_send_sent_event"] is True
    assert conf["beat_max_loop_interval"] == celery_settings().beat_max_loop_interval
