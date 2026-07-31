"""Settings: env group, host-agreeing defaults, and the Redis key helpers."""

from __future__ import annotations

from tai42_backend_rq.settings import RqSettings, rq_settings


def test_env_prefix_is_rq():
    assert RqSettings.model_config.get("env_prefix") == "RQ_"


def test_defaults_agree_with_host_backend_settings():
    """These three mirror the host's generic backend settings — the
    tool-dispatch seam must meet on the same values without configuration."""
    settings = RqSettings()
    assert settings.manifest_key == "MANIFEST_KEY"
    assert settings.task_timeout == 300
    assert settings.tool_name_arg == "backend_tool_name"


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
