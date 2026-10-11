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
    # The dispatch job budget bounds every task the worker runs, dispatched or beat-fired.
    assert conf["task_time_limit"] == celery_settings().task_timeout


def test_create_celery_app_namespaces_redbeat_from_settings(monkeypatch) -> None:
    """The RedBeat schedule store's key prefix comes from the setting, so the
    harness can namespace each stack's beat store on a shared logical DB — a
    leaked beat on a re-leased DB cannot then fire this stack's schedules."""
    from tai42_backend_celery.core.settings import CelerySettings

    monkeypatch.setattr(
        app_module,
        "celery_settings",
        lambda: CelerySettings(redbeat_key_prefix="tai42_e2e_abc123:redbeat:"),
    )
    app = app_module.create_celery_app()
    assert app.conf["redbeat_key_prefix"] == "tai42_e2e_abc123:redbeat:"


def test_the_job_budget_follows_the_dispatch_setting(monkeypatch) -> None:
    from tai42_backend_celery.core.settings import CelerySettings

    monkeypatch.setattr(app_module, "celery_settings", lambda: CelerySettings(task_timeout=77))
    assert app_module.create_celery_app().conf["task_time_limit"] == 77
