"""The checkpoint provider table and the retention-horizon helpers derived from it."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from tai42_kit.llm.checkpoint import providers as providers_mod
from tai42_kit.llm.checkpoint.providers import (
    CHECKPOINT_PROVIDERS,
    CheckpointProviderFacts,
    CheckpointSharedValuesError,
    UnknownCheckpointProviderError,
    checkpoint_park_horizon,
    checkpoint_provider_facts,
    durable_checkpoint_providers,
    shared_value_threshold,
)
from tai42_kit.settings import reset_all_settings

SWITCH = "LLM_PROVIDER_CHECKPOINT_SHARED_VALUES"


def test_provider_table_states_each_provider_facts():
    assert CHECKPOINT_PROVIDERS["redis"] == CheckpointProviderFacts(
        durable=True,
        retention="native_ttl",
        faithful_codec=True,
        inline_compress=True,
        value_store=True,
        shared_values=True,
    )
    assert CHECKPOINT_PROVIDERS["postgres"] == CheckpointProviderFacts(
        durable=True,
        retention="sweep",
        faithful_codec=False,
        inline_compress=True,
        value_store=True,
        shared_values=False,
    )
    assert CHECKPOINT_PROVIDERS["sqlite"] == CheckpointProviderFacts(
        durable=False,
        retention="sweep",
        faithful_codec=False,
        inline_compress=False,
        value_store=False,
        shared_values=False,
    )
    assert CHECKPOINT_PROVIDERS["memory"] == CheckpointProviderFacts(
        durable=False,
        retention="process",
        faithful_codec=False,
        inline_compress=False,
        value_store=False,
        shared_values=False,
    )


def test_a_default_on_needs_a_value_store():
    with pytest.raises(ValueError, match="stores values once by default but keeps no value store"):
        CheckpointProviderFacts(
            durable=True,
            retention="sweep",
            faithful_codec=False,
            inline_compress=False,
            value_store=False,
            shared_values=True,
        )


def test_provider_table_is_read_only():
    with pytest.raises(TypeError):
        CHECKPOINT_PROVIDERS["other"] = CHECKPOINT_PROVIDERS["memory"]  # type: ignore[index]


def test_facts_of_a_known_provider():
    assert checkpoint_provider_facts("redis") is CHECKPOINT_PROVIDERS["redis"]


def test_unknown_provider_is_refused_with_the_known_list():
    with pytest.raises(UnknownCheckpointProviderError) as excinfo:
        checkpoint_provider_facts("cassandra")
    assert str(excinfo.value) == "Unsupported checkpoint provider: 'cassandra'; known: memory, postgres, redis, sqlite"
    assert isinstance(excinfo.value, ValueError)


def test_durable_providers():
    assert durable_checkpoint_providers() == frozenset({"redis", "postgres"})


@pytest.mark.parametrize("provider", ["redis", "postgres"])
def test_park_horizon_of_a_durable_provider_is_the_waiting_retention(monkeypatch, provider):
    monkeypatch.setattr(
        providers_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(checkpoint_retention_waiting_minutes=2880),
    )
    assert checkpoint_park_horizon(provider) == timedelta(minutes=2880)


@pytest.mark.parametrize("provider", ["sqlite", "memory"])
def test_park_horizon_of_a_non_durable_provider_is_none(provider):
    assert checkpoint_park_horizon(provider) is None


def test_park_horizon_refuses_an_unknown_provider():
    with pytest.raises(UnknownCheckpointProviderError):
        checkpoint_park_horizon("cassandra")


@pytest.mark.parametrize("provider", ["redis", "postgres"])
def test_park_horizon_of_a_durable_provider_is_a_declared_retention_waiting(monkeypatch, provider):
    from tai42_kit.llm.checkpoint import ThreadRetention

    monkeypatch.setattr(
        providers_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(checkpoint_retention_waiting_minutes=2880),
    )
    assert checkpoint_park_horizon(provider, ThreadRetention(waiting_minutes=2, finished_minutes=1)) == timedelta(
        minutes=2
    )
    assert checkpoint_park_horizon(provider, None) == timedelta(minutes=2880)


@pytest.mark.parametrize("provider", ["sqlite", "memory"])
def test_park_horizon_of_a_non_durable_provider_is_none_whatever_the_retention(provider):
    from tai42_kit.llm.checkpoint import ThreadRetention

    assert checkpoint_park_horizon(provider, ThreadRetention(2, 1)) is None


# --------------------------------------------------------------------------- #
# shared_value_threshold: the provider's default, the deployment's switch, the size
# --------------------------------------------------------------------------- #
@pytest.fixture
def switch(monkeypatch):
    """Set the deployment switch (``None`` leaves it unset) and read the settings afresh."""

    def set_switch(value: str | None, size: str | None = None) -> None:
        if value is None:
            monkeypatch.delenv(SWITCH, raising=False)
        else:
            monkeypatch.setenv(SWITCH, value)
        if size is not None:
            monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES", size)
        reset_all_settings()

    yield set_switch
    reset_all_settings()


@pytest.mark.parametrize(
    ("provider", "expected"), [("redis", 65536), ("postgres", None), ("sqlite", None), ("memory", None)]
)
def test_unset_takes_the_providers_default(switch, provider, expected):
    switch(None)
    assert shared_value_threshold(provider) == expected


def test_the_switch_off_stores_every_value_inline_on_redis(switch):
    switch("false")
    assert shared_value_threshold("redis") is None


def test_the_switch_on_stores_values_once_on_postgres(switch):
    switch("true")
    assert shared_value_threshold("postgres") == 65536


def test_the_switch_on_returns_the_configured_size(switch):
    switch("true", size="2048")
    assert shared_value_threshold("postgres") == 2048
    assert shared_value_threshold("redis") == 2048


@pytest.mark.parametrize("provider", ["sqlite", "memory"])
def test_the_switch_on_a_provider_without_a_value_store_is_refused_naming_both(switch, provider):
    switch("true")
    with pytest.raises(CheckpointSharedValuesError) as excinfo:
        shared_value_threshold(provider)
    assert SWITCH in str(excinfo.value)
    assert repr(provider) in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)


@pytest.mark.parametrize("provider", ["sqlite", "memory"])
def test_the_switch_off_on_a_provider_without_a_value_store_is_inline(switch, provider):
    switch("false")
    assert shared_value_threshold(provider) is None


def test_shared_value_threshold_refuses_an_unknown_provider(switch):
    switch(None)
    with pytest.raises(UnknownCheckpointProviderError):
        shared_value_threshold("cassandra")
