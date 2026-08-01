"""CelerySettings: defaults, env prefix, and the RedBeat key helpers."""

from __future__ import annotations

import os

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


# -- TAI_DEFAULT_* fallback -------------------------------------------------------

# Env prefixes the fallback tests touch — stripped so ambient env never colours a
# resolution.
_TEST_ENV_PREFIXES = ("CELERY_", "TAI_DEFAULT_")


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every relevant env var so the test controls the whole set."""
    for key in list(os.environ):
        if key.startswith(_TEST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.mark.usefixtures("isolated_env")
def test_all_three_urls_fall_back_to_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    # broker_url, result_backend, and redbeat_redis_url all map to
    # TAI_DEFAULT_REDIS_URL — with the default set and the CELERY_ vars unset the
    # broker resolves to Redis rather than the AMQP class default.
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    settings = CelerySettings()
    assert settings.broker_url == "redis://shared:6379/0"
    assert settings.result_backend == "redis://shared:6379/0"
    assert settings.redbeat_redis_url == "redis://shared:6379/0"


@pytest.mark.usefixtures("isolated_env")
def test_specific_broker_url_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_BROKER_URL", "amqp://broker.example:5672//")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    settings = CelerySettings()
    assert settings.broker_url == "amqp://broker.example:5672//"  # specific wins
    assert settings.result_backend == "redis://shared:6379/0"  # unset — default fills it


@pytest.mark.usefixtures("isolated_env")
def test_nothing_set_keeps_class_defaults() -> None:
    settings = CelerySettings()
    assert settings.broker_url == "amqp://localhost:5672//"
    assert settings.result_backend == "redis://localhost:6379/0"
    assert settings.redbeat_redis_url == "redis://localhost:6379/0"
