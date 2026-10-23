"""Settings: env group, host-agreeing defaults, and the Redis key helpers."""

from __future__ import annotations

import os

import pytest
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import registered_settings

from tai42_backend_rq.settings import RqRedisSettings, RqSettings, rq_settings


def _rq_group(name: str = "RqSettings"):
    """An ``RQ_`` env group as the settings registry sees it."""
    return next(info for info in registered_settings() if info.name == name)


def test_env_prefix_is_rq():
    assert RqSettings.model_config.get("env_prefix") == "RQ_"


def test_defaults_agree_with_host_backend_settings():
    """These three are INHERITED from the shared dispatch group rather than
    mirrored, so the tool-dispatch seam meets on the same values by
    construction — the env surface under ``RQ_`` is unchanged."""
    assert issubclass(RqSettings, BackendDispatchSettings)
    settings = RqSettings()
    assert settings.manifest_key == "MANIFEST_KEY"
    assert settings.task_timeout == 300
    assert settings.tool_name_arg == "backend_tool_name"


def test_the_dispatch_group_still_registers_under_the_rq_prefix():
    """The shared group excludes ITSELF from the registry (own-attribute flag),
    so this concrete side still registers, with its own prefixed env vars."""
    fields = {field.name: field for field in _rq_group().fields}
    assert fields["manifest_key"].env_var == "RQ_MANIFEST_KEY"
    assert fields["tool_name_arg"].env_var == "RQ_TOOL_NAME_ARG"


def test_the_redis_connection_registers_its_own_rq_names():
    fields = {field.name: field for field in _rq_group("RqRedisSettings").fields}
    assert fields["redis_url"].env_var == "RQ_REDIS_URL"
    assert fields["redis_max_connections"].env_var == "RQ_REDIS_MAX_CONNECTIONS"
    assert fields["retry_attempts"].env_var == "RQ_RETRY_ATTEMPTS"
    assert fields["retry_on_timeout"].env_var == "RQ_RETRY_ON_TIMEOUT"
    assert fields["socket_timeout"].env_var == "RQ_SOCKET_TIMEOUT"
    assert fields["socket_connect_timeout"].env_var == "RQ_SOCKET_CONNECT_TIMEOUT"


def test_boot_pinned_dispatch_fields_are_recycle_class():
    """Inheriting the shared group brings the truthful reload classes with it: a
    forked work-horse reads the manifest env key it was GIVEN and a queued job
    carries the kwarg name its producer used, so neither converges in-process.
    A worker binds its job budget when it starts, so it converges through a recycle too."""
    fields = {field.name: field for field in _rq_group().fields}
    assert fields["manifest_key"].reload_class == "recycle"
    assert fields["tool_name_arg"].reload_class == "recycle"
    assert fields["task_timeout"].reload_class == "recycle"


def test_broker_defaults():
    settings = RqSettings()
    assert isinstance(settings.redis, RqRedisSettings)
    assert settings.redis.redis_url == "redis://localhost:6379/0"
    assert settings.rq_prefix == "rq:"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("RQ_REDIS_URL", "redis://elsewhere:6380/2")
    monkeypatch.setenv("RQ_TASK_TIMEOUT", "60")
    settings = RqSettings()
    assert settings.redis.redis_url == "redis://elsewhere:6380/2"
    assert settings.task_timeout == 60


@pytest.mark.usefixtures("isolated_env")
def test_the_retry_timeouts_and_cap_load_from_the_rq_names(monkeypatch):
    monkeypatch.setenv("RQ_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("RQ_RETRY_ON_TIMEOUT", "true")
    monkeypatch.setenv("RQ_SOCKET_TIMEOUT", "3")
    monkeypatch.setenv("RQ_SOCKET_CONNECT_TIMEOUT", "4")
    monkeypatch.setenv("RQ_REDIS_MAX_CONNECTIONS", "9")

    redis = RqSettings().redis
    assert redis.retry_attempts == 2
    assert redis.retry_on_timeout is True
    assert redis.socket_timeout == 3
    assert redis.socket_connect_timeout == 4
    assert redis.redis_max_connections == 9


@pytest.mark.usefixtures("isolated_env")
def test_redis_max_connections_falls_back_to_default_namespace(monkeypatch):
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    assert RqSettings().redis.redis_max_connections == 42


def test_key_helpers():
    settings = RqSettings()
    assert settings.rq_scheduler_zset == "rq:scheduler:scheduled_jobs"
    assert settings.rq_job_key("j1") == "rq:job:j1"
    assert settings.rq_job_dependencies("j1") == "rq:job::j1:dependencies"
    assert settings.rq_result_key("j1") == "rq:results:j1"
    assert settings.rq_worker_key("w1") == "rq:worker:w1"
    assert settings.rq_workers_key == "rq:workers"
    assert settings.rq_queues_key == "rq:queues"
    assert settings.rq_queue_key("default") == "rq:queue:default"
    assert settings.rq_scheduled_registry_key("default") == "rq:scheduled:default"


def test_settings_accessor_is_cached():
    assert rq_settings() is rq_settings()


# -- TAI_DEFAULT_* fallback -------------------------------------------------------

# Env prefixes the fallback tests touch — stripped so ambient env never colours a
# resolution.
_TEST_ENV_PREFIXES = ("RQ_", "TAI_DEFAULT_")


@pytest.fixture
def isolated_env(monkeypatch):
    """Drop every relevant env var so the test controls the whole set."""
    for key in list(os.environ):
        if key.startswith(_TEST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.mark.usefixtures("isolated_env")
def test_redis_url_falls_back_to_default_namespace(monkeypatch):
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert RqSettings().redis.redis_url == "redis://shared:6379/0"


@pytest.mark.usefixtures("isolated_env")
def test_specific_redis_url_beats_default(monkeypatch):
    monkeypatch.setenv("RQ_REDIS_URL", "redis://rq:6380/1")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert RqSettings().redis.redis_url == "redis://rq:6380/1"


@pytest.mark.usefixtures("isolated_env")
def test_nothing_set_keeps_class_default():
    redis = RqSettings().redis
    assert redis.redis_url == "redis://localhost:6379/0"
    assert redis.redis_max_connections is None
    assert redis.retry_attempts == 0


def test_queue_name_default_is_rq_default():
    # RQ's own default queue, so an unconfigured deployment is unchanged.
    assert RqSettings().queue_name == "default"


@pytest.mark.usefixtures("isolated_env")
def test_queue_name_is_read_from_env(monkeypatch):
    monkeypatch.setenv("RQ_QUEUE_NAME", "tai42_e2e_abc123:default")
    assert RqSettings().queue_name == "tai42_e2e_abc123:default"


def test_the_redis_connection_publishes_exactly_its_connection_settings() -> None:
    env_vars = {field.env_var for field in _rq_group("RqRedisSettings").fields}
    assert env_vars == {
        "RQ_REDIS_URL",
        "RQ_REDIS_MAX_CONNECTIONS",
        "RQ_RETRY_ATTEMPTS",
        "RQ_RETRY_ON_TIMEOUT",
        "RQ_SOCKET_TIMEOUT",
        "RQ_SOCKET_CONNECT_TIMEOUT",
    }
