"""``LLMProviderSettings`` owns the ``LLM_PROVIDER_`` env prefix.

An env name under the prefix that no registered settings group accepts is refused by the owned-prefix
audit, so a mistyped or unknown checkpoint setting is loud instead of ignored.
"""

from collections.abc import Iterator

import pytest

from tai42_kit.llm.settings import LLMProviderSettings
from tai42_kit.settings import UnknownOwnedSettingError, owned_env_prefixes, refuse_unknown_owned_env
from tai42_kit.settings import registry as registry_mod


@pytest.fixture(autouse=True)
def _only_the_provider_group() -> Iterator[None]:
    """Audit against the provider group alone, then restore every registration."""
    saved = dict(registry_mod._REGISTRY)
    registry_mod._clear_registry()
    registry_mod._register(LLMProviderSettings)
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
            "LLM_PROVIDER_STORE",
            "LLM_PROVIDER_STORE_CONN_STRING",
        ],
        source="boot",
    )
