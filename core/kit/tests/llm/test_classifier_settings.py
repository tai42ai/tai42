"""``ClassifierSettings`` reads the ``CLASSIFIER_`` namespace; the provider default is typesafe.

Settings-test style: set the env, construct the group, assert the fields. The
``api_key`` stays a ``SecretStr`` so it is masked upstream of the build seam.
"""

import pytest

pytest.importorskip("langgraph")

from pydantic import SecretStr

from tai42_kit.llm.settings import ClassifierSettings, LLMProviderSettings


def test_defaults():
    settings = ClassifierSettings()
    assert settings.model == "jev-latest"
    assert settings.base_url is None
    assert settings.api_key is None
    assert settings.timeout is None


def test_reads_classifier_env_namespace(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_MODEL", "jev-2")
    monkeypatch.setenv("CLASSIFIER_BASE_URL", "http://host:9000")
    monkeypatch.setenv("CLASSIFIER_API_KEY", "sk-classifier")
    monkeypatch.setenv("CLASSIFIER_TIMEOUT", "45")

    settings = ClassifierSettings()
    assert settings.model == "jev-2"
    assert settings.base_url == "http://host:9000"
    assert isinstance(settings.api_key, SecretStr)
    assert settings.api_key.get_secret_value() == "sk-classifier"
    assert settings.timeout == 45


def test_api_key_is_masked_in_repr(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_API_KEY", "sk-classifier")
    assert "sk-classifier" not in repr(ClassifierSettings())


def test_with_fallbacks_drops_none_and_lets_caller_win(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_MODEL", "jev-2")
    merged = ClassifierSettings().with_fallbacks({"base_url": "http://override"})
    # None fields (api_key, timeout) are dropped so they never reach the provider ctor.
    assert "api_key" not in merged
    assert "timeout" not in merged
    assert merged["model"] == "jev-2"
    assert merged["base_url"] == "http://override"


def test_provider_default_is_typesafe():
    assert LLMProviderSettings().classifier == "typesafe"


def test_provider_reads_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CLASSIFIER", "other")
    assert LLMProviderSettings().classifier == "other"
