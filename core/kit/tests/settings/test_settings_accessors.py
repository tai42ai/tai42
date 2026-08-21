"""The @settings_cache accessor functions build their settings objects and are
registered for the global reset. Calling each with defaults exercises its body;
reset_all_settings afterward keeps the cached singletons from leaking.
"""

import pytest
from pydantic import ValidationError

pytest.importorskip("langgraph")

from tai42_kit.clients.settings import ClientSettings, MCPClientSettings, mcp_client_settings
from tai42_kit.llm import settings as llm_settings_mod
from tai42_kit.logging.settings import LoggingSettings, logging_settings
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data.jq_util import JqSettings, jq_settings


@pytest.fixture(autouse=True)
def _reset_after():
    yield
    reset_all_settings()


def test_client_settings_base_kwargs_not_implemented():
    with pytest.raises(NotImplementedError):
        ClientSettings().client_kwargs()


def test_logging_settings_accessor_builds():
    assert isinstance(logging_settings(), LoggingSettings)


def test_mcp_client_settings_accessor_builds_defaults():
    s = mcp_client_settings()
    assert isinstance(s, MCPClientSettings)
    assert s.connect_timeout_seconds == 30
    assert s.call_timeout_seconds == 300


def test_jq_settings_accessor_builds_defaults():
    s = jq_settings()
    assert isinstance(s, JqSettings)
    assert s.timeout_seconds == 10


def test_llm_settings_accessors_build():
    # Each cached accessor constructs its settings object from defaults.
    assert llm_settings_mod.trimming_middleware_settings().strategy == "last"
    assert llm_settings_mod.context_overflow_settings().methods == ["trimming"]
    assert llm_settings_mod.summarization_middleware_settings().keep_messages == 20
    assert llm_settings_mod.context_editing_settings().keep_tool_results == 3
    assert llm_settings_mod.llm_provider_settings().llm == "openai"
    assert llm_settings_mod.llm_settings().model == "gpt-4o"
    assert llm_settings_mod.embedding_settings().model == "text-embedding-3-small"


def test_unset_temperature_absent_from_provider_kwargs():
    # model_dump(exclude_none) feeds get_llm; an unset sampling param must not
    # reach the provider, so its key is absent entirely (no injected default).
    dumped = llm_settings_mod.LLMSettings().model_dump(exclude_none=True)
    assert "temperature" not in dumped


def test_configured_temperature_passes_through_unchanged():
    dumped = llm_settings_mod.LLMSettings(temperature=0.7).model_dump(exclude_none=True)
    assert dumped["temperature"] == 0.7


def test_checkpoint_ttl_minutes_defaults_to_thirty_days():
    # Retention is bounded out of the box (30 days, mirroring the answer retention
    # window); an operator sets ``None`` to keep every checkpoint forever.
    assert llm_settings_mod.llm_provider_settings().checkpoint_ttl_minutes == 30 * 24 * 60


def test_checkpoint_ttl_minutes_accepts_none_to_keep_forever():
    assert llm_settings_mod.LLMProviderSettings(checkpoint_ttl_minutes=None).checkpoint_ttl_minutes is None


def test_conn_strings_default_to_none():
    # No hidden localhost: an unset conn string falls back per provider to the
    # shared connection namespace at the resource factory, not here.
    provider = llm_settings_mod.LLMProviderSettings()
    assert provider.checkpoint_conn_string is None
    assert provider.store_conn_string is None


def test_checkpoint_ttl_minutes_accepts_a_positive_value():
    s = llm_settings_mod.LLMProviderSettings(checkpoint_ttl_minutes=120)
    assert s.checkpoint_ttl_minutes == 120


@pytest.mark.parametrize("bad", [0, -1, -60])
def test_checkpoint_ttl_minutes_rejects_non_positive(bad):
    # A non-positive TTL is a misconfiguration, rejected loudly rather than read as "off".
    with pytest.raises(ValidationError, match="positive"):
        llm_settings_mod.LLMProviderSettings(checkpoint_ttl_minutes=bad)
