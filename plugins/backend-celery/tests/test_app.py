"""The Celery application factory wires RedBeat, pool restarts, and task events."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from redbeat.schedulers import get_redis
from tai42_kit.clients.impl.redis import _LoggedSyncRetry

import tai42_backend_celery.core.app as app_module
from tai42_backend_celery.core.settings import celery_settings


def test_create_celery_app_wires_redbeat_and_events() -> None:
    conf: Any = app_module.celery_app.conf
    assert conf["beat_scheduler"] == "tai42_backend_celery.core.beat_scheduler.ProgressReportingScheduler"
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


# --- the RedBeat store's connection ---------------------------------------------


@pytest.fixture
def redbeat_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.startswith(("CELERY_", "TAI_DEFAULT_")):
            monkeypatch.delenv(key, raising=False)
    celery_settings.cache_clear()
    yield
    celery_settings.cache_clear()


@pytest.mark.usefixtures("redbeat_env")
def test_redbeat_connects_with_the_kit_connection_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_REDBEAT_REDIS_URL", "redis://beat.example:6390/5")
    monkeypatch.setenv("CELERY_REDBEAT_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("CELERY_REDBEAT_SOCKET_TIMEOUT", "3")
    monkeypatch.setenv("CELERY_REDBEAT_REDIS_MAX_CONNECTIONS", "7")

    app = app_module.create_celery_app()

    options = app.conf.redbeat_redis_options
    assert "url" not in options
    assert options["decode_responses"] is True
    assert options["socket_timeout"] == 3
    assert options["max_connections"] == 7
    assert isinstance(options["retry"], _LoggedSyncRetry)
    assert options["retry"]._budget == 2
    assert options["retry"]._label == "CELERY_REDBEAT_REDIS_URL"

    client = get_redis(app)
    connection_kwargs = client.connection_pool.connection_kwargs
    assert (connection_kwargs["host"], connection_kwargs["port"], connection_kwargs["db"]) == ("beat.example", 6390, 5)
    assert connection_kwargs["retry"] is options["retry"]
    assert connection_kwargs["decode_responses"] is True


@pytest.mark.usefixtures("redbeat_env")
def test_a_redis_app_builds_without_the_base_redis_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker and result backend bring their own URL, so no base Redis URL is required."""
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker.example:6390/1")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://results.example:6390/2")
    monkeypatch.delenv("REDIS_URL", raising=False)

    app = app_module.create_celery_app()

    assert app.backend.client.connection_pool.connection_kwargs["host"] == "results.example"
    connection = app.connection_for_write()
    channel = object.__new__(connection.transport.Channel)
    channel.connection = connection.transport
    assert channel._connparams()["host"] == "broker.example"
