"""Settings: env mapping onto the contract ProjectConfig."""

from __future__ import annotations

import pytest

from tai42_monitoring_langfuse.settings import LangfuseSettings, langfuse_settings


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_TIMEOUT_SECONDS",
        "LANGFUSE_TRACING_ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults(monkeypatch):
    _clear_env(monkeypatch)
    settings = LangfuseSettings()
    assert settings.public_key == ""
    assert settings.secret_key is None
    assert settings.host == ""
    assert settings.timeout_seconds == 30
    assert settings.tracing_environment == "tai"


def test_to_project_config_maps_env_group(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf")
    monkeypatch.setenv("LANGFUSE_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "staging")

    config = LangfuseSettings().to_project_config()
    assert config.public_key == "pk"
    assert config.secret_key == "sk"  # plaintext extracted only at the mapping
    assert config.host == "http://lf"
    assert config.timeout_seconds == 7
    assert config.source == "staging"


def test_secret_key_is_masked_in_repr(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-very-secret")
    settings = LangfuseSettings()
    assert "sk-very-secret" not in repr(settings)
    assert "sk-very-secret" not in str(settings.model_dump())


@pytest.mark.parametrize("missing", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"])
def test_to_project_config_incomplete_raises(monkeypatch, missing):
    _clear_env(monkeypatch)
    for key, value in (
        ("LANGFUSE_PUBLIC_KEY", "pk"),
        ("LANGFUSE_SECRET_KEY", "sk"),
        ("LANGFUSE_HOST", "http://lf"),
    ):
        if key != missing:
            monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="not all set"):
        LangfuseSettings().to_project_config()


def test_cached_accessor_returns_singleton(monkeypatch):
    _clear_env(monkeypatch)
    assert langfuse_settings() is langfuse_settings()
