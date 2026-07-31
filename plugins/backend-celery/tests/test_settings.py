"""CelerySettings: defaults, env prefix, and the RedBeat key helpers."""

from __future__ import annotations

import pytest

from tai42_backend_celery.core.settings import CelerySettings, celery_settings


def test_defaults() -> None:
    settings = CelerySettings()
    assert settings.broker_url == "amqp://localhost:5672//"
    assert settings.result_backend == "redis://localhost:6379/0"
    assert settings.redbeat_redis_url == "redis://localhost:6379/0"
    assert settings.redbeat_key_prefix == "redbeat:"
    assert settings.beat_max_loop_interval == 60


def test_base_backend_fields_agree_with_host_defaults() -> None:
    settings = CelerySettings()
    assert settings.tool_name_arg == "backend_tool_name"
    assert settings.task_timeout == 300
    assert settings.manifest_key == "MANIFEST_KEY"


def test_redbeat_schedule_key_is_double_colon() -> None:
    """The schedule zset key is prefix + ':schedule' — 'redbeat::schedule'
    exactly, matching the key RedBeat maintains."""
    assert CelerySettings().redbeat_schedule_key == "redbeat::schedule"


def test_redbeat_task_key_prefixes_once() -> None:
    settings = CelerySettings()
    assert settings.redbeat_task_key("job") == "redbeat:job"
    assert settings.redbeat_task_key("redbeat:job") == "redbeat:job"


def test_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_BROKER_URL", "amqp://broker.example:5672//")
    monkeypatch.setenv("CELERY_TASK_TIMEOUT", "17")
    settings = CelerySettings()
    assert settings.broker_url == "amqp://broker.example:5672//"
    assert settings.task_timeout == 17


def test_accessor_is_cached() -> None:
    celery_settings.cache_clear()
    assert celery_settings() is celery_settings()
    celery_settings.cache_clear()
