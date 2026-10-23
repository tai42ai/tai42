"""CelerySettings: defaults, env prefix, and the RedBeat key helpers."""

from __future__ import annotations

import os

import pytest
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import registered_settings

from tai42_backend_celery.core.settings import (
    CeleryBrokerRedisOptions,
    CeleryRedbeatRedisSettings,
    CeleryResultBackendRedisOptions,
    CelerySettings,
    celery_settings,
)


def test_defaults() -> None:
    settings = CelerySettings()
    assert settings.broker_url == "amqp://localhost:5672//"
    assert settings.result_backend == "redis://localhost:6379/0"
    assert isinstance(settings.redbeat, CeleryRedbeatRedisSettings)
    assert settings.redbeat.redis_url == "redis://localhost:6379/0"
    assert settings.redbeat_key_prefix == "redbeat:"
    assert settings.beat_max_loop_interval == 60


def test_base_backend_fields_agree_with_host_defaults() -> None:
    # Inherited from the shared mixin, so this side and the host cannot drift on
    # the names or the defaults.
    assert issubclass(CelerySettings, BackendDispatchSettings)
    settings = CelerySettings()
    assert settings.tool_name_arg == "backend_tool_name"
    assert settings.task_timeout == 300
    assert settings.manifest_key == "MANIFEST_KEY"


@pytest.mark.parametrize("field", ["manifest_key", "tool_name_arg"])
def test_the_dispatch_fields_are_recycle_class(field: str) -> None:
    # Both pin wiring built at boot — a forked child reads the env key it was
    # given, a queued job carries the kwarg name its producer used — so a change
    # converges through a process recycle, not an in-process flip.
    extra = CelerySettings.model_fields[field].json_schema_extra
    assert isinstance(extra, dict)
    assert extra["reload"] == "recycle"


def test_the_concrete_group_still_registers() -> None:
    # ``registry_exclude`` on the abstract mixin is an own-attribute flag, so the
    # ``CELERY_`` group is a real settings group despite inheriting it — while the
    # unprefixed mixin itself never registers.
    assert CelerySettings.__dict__.get("registry_exclude") is None
    registered = {info.name for info in registered_settings()}
    assert "CelerySettings" in registered
    assert "BackendDispatchSettings" not in registered


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
    # broker_url, result_backend, and the RedBeat store's redis_url all fall back to
    # TAI_DEFAULT_REDIS_URL — with the default set and the CELERY_ vars unset the
    # broker resolves to Redis rather than the AMQP class default.
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    settings = CelerySettings()
    assert settings.broker_url == "redis://shared:6379/0"
    assert settings.result_backend == "redis://shared:6379/0"
    assert settings.redbeat.redis_url == "redis://shared:6379/0"


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
    assert settings.redbeat.redis_url == "redis://localhost:6379/0"


# -- Redis connection namespaces ---------------------------------------------------


def _group(name: str):
    return next(info for info in registered_settings() if info.name == name)


@pytest.mark.usefixtures("isolated_env")
def test_the_redbeat_connection_loads_from_its_own_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_REDBEAT_REDIS_URL", "redis://beat:6379/5")
    monkeypatch.setenv("CELERY_REDBEAT_REDIS_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("CELERY_REDBEAT_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("CELERY_REDBEAT_RETRY_ON_TIMEOUT", "true")
    monkeypatch.setenv("CELERY_REDBEAT_SOCKET_TIMEOUT", "3")
    monkeypatch.setenv("CELERY_REDBEAT_SOCKET_CONNECT_TIMEOUT", "4")

    redbeat = CelerySettings().redbeat
    assert redbeat.redis_url == "redis://beat:6379/5"
    assert redbeat.redis_max_connections == 7
    assert redbeat.retry_attempts == 2
    assert redbeat.retry_on_timeout is True
    assert redbeat.socket_timeout == 3
    assert redbeat.socket_connect_timeout == 4


@pytest.mark.usefixtures("isolated_env")
def test_the_redbeat_connection_falls_back_to_the_shared_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "11")

    assert CelerySettings().redbeat.redis_max_connections == 11


def test_the_redbeat_connection_registers_its_own_names() -> None:
    fields = {field.name: field.env_var for field in _group("CeleryRedbeatRedisSettings").fields}
    assert fields["redis_url"] == "CELERY_REDBEAT_REDIS_URL"
    assert fields["redis_max_connections"] == "CELERY_REDBEAT_REDIS_MAX_CONNECTIONS"
    assert fields["retry_attempts"] == "CELERY_REDBEAT_RETRY_ATTEMPTS"
    assert fields["retry_on_timeout"] == "CELERY_REDBEAT_RETRY_ON_TIMEOUT"
    assert fields["socket_timeout"] == "CELERY_REDBEAT_SOCKET_TIMEOUT"
    assert fields["socket_connect_timeout"] == "CELERY_REDBEAT_SOCKET_CONNECT_TIMEOUT"


@pytest.mark.usefixtures("isolated_env")
def test_the_result_backend_and_broker_options_load_from_their_own_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELERY_RESULT_BACKEND_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("CELERY_RESULT_BACKEND_SOCKET_TIMEOUT", "3")
    monkeypatch.setenv("CELERY_BROKER_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("CELERY_BROKER_SOCKET_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    settings = CelerySettings()
    assert isinstance(settings.result_backend_redis, CeleryResultBackendRedisOptions)
    assert isinstance(settings.broker_redis, CeleryBrokerRedisOptions)
    assert settings.result_backend_redis.connection_options() == {"retry_attempts": 2, "socket_timeout": 3.0}
    assert settings.broker_redis.connection_options() == {"retry_attempts": 4, "socket_connect_timeout": 5.0}


@pytest.mark.parametrize("cls", [CeleryResultBackendRedisOptions, CeleryBrokerRedisOptions])
def test_the_result_backend_and_broker_options_carry_no_url(cls: type) -> None:
    # Their URL is the backend's / broker's own string; no URL field, no shared fallback.
    assert "redis_url" not in cls.model_fields
    assert not getattr(cls, "tai_default_fields", {})


def test_the_result_backend_and_broker_options_register_their_own_names() -> None:
    backend = {field.name: field.env_var for field in _group("CeleryResultBackendRedisOptions").fields}
    broker = {field.name: field.env_var for field in _group("CeleryBrokerRedisOptions").fields}
    assert backend["retry_attempts"] == "CELERY_RESULT_BACKEND_RETRY_ATTEMPTS"
    assert backend["socket_timeout"] == "CELERY_RESULT_BACKEND_SOCKET_TIMEOUT"
    assert broker["retry_attempts"] == "CELERY_BROKER_RETRY_ATTEMPTS"
    assert broker["retry_on_timeout"] == "CELERY_BROKER_RETRY_ON_TIMEOUT"


def test_the_redis_connection_publishes_exactly_its_connection_settings() -> None:
    env_vars = {field.env_var for field in _group("CeleryRedbeatRedisSettings").fields}
    assert env_vars == {
        "CELERY_REDBEAT_REDIS_URL",
        "CELERY_REDBEAT_REDIS_MAX_CONNECTIONS",
        "CELERY_REDBEAT_RETRY_ATTEMPTS",
        "CELERY_REDBEAT_RETRY_ON_TIMEOUT",
        "CELERY_REDBEAT_SOCKET_TIMEOUT",
        "CELERY_REDBEAT_SOCKET_CONNECT_TIMEOUT",
    }
