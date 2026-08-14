"""Settings: env group, host-agreeing defaults, and the Redis key helpers."""

from __future__ import annotations

import os

import pytest
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import registered_settings

from tai42_backend_rq.settings import RqSettings, rq_settings


def _rq_group():
    """The ``RQ_`` env group as the settings registry sees it."""
    return next(info for info in registered_settings() if info.name == "RqSettings")


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
    assert fields["redis_url"].env_var == "RQ_REDIS_URL"


def test_boot_pinned_dispatch_fields_are_recycle_class():
    """Inheriting the shared group brings the truthful reload classes with it: a
    forked work-horse reads the manifest env key it was GIVEN and a queued job
    carries the kwarg name its producer used, so neither converges in-process.
    The dispatch result timeout is read per call, so it stays hot."""
    fields = {field.name: field for field in _rq_group().fields}
    assert fields["manifest_key"].reload_class == "recycle"
    assert fields["tool_name_arg"].reload_class == "recycle"
    assert fields["task_timeout"].reload_class == "hot"


def test_broker_defaults():
    settings = RqSettings()
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.rq_prefix == "rq:"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("RQ_REDIS_URL", "redis://elsewhere:6380/2")
    monkeypatch.setenv("RQ_TASK_TIMEOUT", "60")
    settings = RqSettings()
    assert settings.redis_url == "redis://elsewhere:6380/2"
    assert settings.task_timeout == 60


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

    assert RqSettings().redis_url == "redis://shared:6379/0"


@pytest.mark.usefixtures("isolated_env")
def test_specific_redis_url_beats_default(monkeypatch):
    monkeypatch.setenv("RQ_REDIS_URL", "redis://rq:6380/1")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    assert RqSettings().redis_url == "redis://rq:6380/1"


@pytest.mark.usefixtures("isolated_env")
def test_nothing_set_keeps_class_default():
    assert RqSettings().redis_url == "redis://localhost:6379/0"
