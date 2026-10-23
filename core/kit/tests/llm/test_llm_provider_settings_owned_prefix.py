"""``LLMProviderSettings`` owns the ``LLM_PROVIDER_`` env prefix.

An env name under the prefix that no registered settings group accepts is refused by the owned-prefix
audit, so a mistyped or unknown checkpoint setting is loud instead of ignored.
"""

from collections.abc import Iterator

import pytest

from tai42_kit.llm import settings as llm_settings_mod
from tai42_kit.llm.settings import LlmCheckpointConnectionOptions, LLMProviderSettings, LlmStoreConnectionOptions
from tai42_kit.settings import (
    UnknownOwnedSettingError,
    owned_env_prefixes,
    refuse_unknown_owned_env,
    reset_all_settings,
)
from tai42_kit.settings import registry as registry_mod


@pytest.fixture(autouse=True)
def _only_the_provider_group() -> Iterator[None]:
    """Audit against the groups under the prefix alone, then restore every registration."""
    saved = dict(registry_mod._REGISTRY)
    registry_mod._clear_registry()
    registry_mod._register(LLMProviderSettings)
    registry_mod._register(LlmStoreConnectionOptions)
    registry_mod._register(LlmCheckpointConnectionOptions)
    yield
    registry_mod._clear_registry()
    registry_mod._REGISTRY.update(saved)


def test_the_provider_group_owns_its_prefix() -> None:
    assert LLMProviderSettings.env_prefix_owned is True
    assert owned_env_prefixes() == frozenset({"LLM_PROVIDER_"})


def test_an_unknown_checkpoint_setting_under_the_prefix_is_refused_naming_it() -> None:
    with pytest.raises(
        UnknownOwnedSettingError,
        match=r"^boot: unknown setting\(s\) under an owned env prefix: "
        r"LLM_PROVIDER_CHECKPOINT_TTL_MINUTES \(prefix LLM_PROVIDER_\)\.",
    ):
        refuse_unknown_owned_env({"LLM_PROVIDER_CHECKPOINT_TTL_MINUTES": "43200"}, source="boot")


def test_a_misspelled_shared_value_setting_is_refused_naming_it() -> None:
    with pytest.raises(UnknownOwnedSettingError, match=r"LLM_PROVIDER_CHECKPOINT_SHARED_VALUES_BYTES \(prefix"):
        refuse_unknown_owned_env({"LLM_PROVIDER_CHECKPOINT_SHARED_VALUES_BYTES": "1024"}, source="boot")


def test_a_misspelled_shared_values_switch_is_refused_naming_it() -> None:
    with pytest.raises(UnknownOwnedSettingError, match=r"LLM_PROVIDER_CHECKPOINT_SHARED_VALUE \(prefix"):
        refuse_unknown_owned_env({"LLM_PROVIDER_CHECKPOINT_SHARED_VALUE": "true"}, source="boot")


def test_every_setting_of_the_group_passes() -> None:
    refuse_unknown_owned_env(
        [
            "LLM_PROVIDER_LLM",
            "LLM_PROVIDER_EMBEDDING",
            "LLM_PROVIDER_CLASSIFIER",
            "LLM_PROVIDER_CHECKPOINT",
            "LLM_PROVIDER_CHECKPOINT_CONN_STRING",
            "LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES",
            "LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES",
            "LLM_PROVIDER_CHECKPOINT_INLINE_COMPRESS_BYTES",
            "LLM_PROVIDER_CHECKPOINT_SHARED_VALUES",
            "LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES",
            "LLM_PROVIDER_STORE",
            "LLM_PROVIDER_STORE_CONN_STRING",
            "LLM_PROVIDER_STORE_RETRY_ATTEMPTS",
            "LLM_PROVIDER_STORE_RETRY_ON_TIMEOUT",
            "LLM_PROVIDER_STORE_SOCKET_TIMEOUT",
            "LLM_PROVIDER_STORE_SOCKET_CONNECT_TIMEOUT",
            "LLM_PROVIDER_STORE_PG_RETRY_ATTEMPTS",
            "LLM_PROVIDER_CHECKPOINT_RETRY_ATTEMPTS",
            "LLM_PROVIDER_CHECKPOINT_RETRY_ON_TIMEOUT",
            "LLM_PROVIDER_CHECKPOINT_SOCKET_TIMEOUT",
            "LLM_PROVIDER_CHECKPOINT_SOCKET_CONNECT_TIMEOUT",
            "LLM_PROVIDER_CHECKPOINT_PG_RETRY_ATTEMPTS",
        ],
        source="boot",
    )


@pytest.mark.parametrize(
    ("prefix", "accessor"),
    [
        ("LLM_PROVIDER_STORE_", llm_settings_mod.llm_store_connection_options),
        ("LLM_PROVIDER_CHECKPOINT_", llm_settings_mod.llm_checkpoint_connection_options),
    ],
)
def test_a_conn_string_doors_options_load_from_its_own_names(monkeypatch, prefix, accessor) -> None:
    monkeypatch.setenv(f"{prefix}RETRY_ATTEMPTS", "2")
    monkeypatch.setenv(f"{prefix}RETRY_ON_TIMEOUT", "true")
    monkeypatch.setenv(f"{prefix}SOCKET_TIMEOUT", "3")
    monkeypatch.setenv(f"{prefix}SOCKET_CONNECT_TIMEOUT", "1.5")
    monkeypatch.setenv(f"{prefix}PG_RETRY_ATTEMPTS", "4")
    reset_all_settings()
    options = accessor()
    assert options.connection_options() == {
        "socket_timeout": 3.0,
        "socket_connect_timeout": 1.5,
        "retry_on_timeout": True,
        "retry_attempts": 2,
    }
    assert options.pg_retry_attempts == 4
