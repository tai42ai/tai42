"""The checkpoint provider table and the retention-horizon helpers derived from it."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from tai42_kit.llm.checkpoint import providers as providers_mod
from tai42_kit.llm.checkpoint.providers import (
    CHECKPOINT_PROVIDERS,
    CheckpointProviderFacts,
    UnknownCheckpointProviderError,
    checkpoint_park_horizon,
    checkpoint_provider_facts,
    durable_checkpoint_providers,
)


def test_provider_table_states_each_provider_facts():
    assert CHECKPOINT_PROVIDERS["redis"] == CheckpointProviderFacts(
        durable=True, retention="native_ttl", faithful_codec=True, inline_compress=True
    )
    assert CHECKPOINT_PROVIDERS["postgres"] == CheckpointProviderFacts(
        durable=True, retention="sweep", faithful_codec=False, inline_compress=True
    )
    assert CHECKPOINT_PROVIDERS["sqlite"] == CheckpointProviderFacts(
        durable=False, retention="sweep", faithful_codec=False, inline_compress=False
    )
    assert CHECKPOINT_PROVIDERS["memory"] == CheckpointProviderFacts(
        durable=False, retention="process", faithful_codec=False, inline_compress=False
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
