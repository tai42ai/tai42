"""The checkpoint provider registry: each store registers its facts and hooks; the retention helpers read them."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm.checkpoint import (
    CheckpointProvider,
    CheckpointProviderFacts,
    UnknownCheckpointProviderError,
    checkpoint_park_horizon,
    checkpoint_provider,
    checkpoint_provider_facts,
    checkpoint_providers,
    durable_checkpoint_providers,
    register_checkpoint_provider,
    shared_value_threshold,
)
from tai42_kit.llm.checkpoint import providers as providers_mod
from tai42_kit.settings import reset_all_settings

SWITCH = "LLM_PROVIDER_CHECKPOINT_SHARED_VALUES"


@pytest.fixture
def registry() -> Iterator[None]:
    """Restore the registry after a test that registers its own providers."""
    held = dict(providers_mod._PROVIDERS._generation.committed())
    instances = dict(providers_mod._instances)
    yield
    committed = providers_mod._PROVIDERS._generation.committed()
    committed.clear()
    committed.update(held)
    providers_mod._instances.clear()
    providers_mod._instances.update(instances)


class _Hooks(CheckpointProvider):
    """Every abstract hook, unused by these tests."""

    async def open(self, conn_string: str | None) -> Any:
        raise NotImplementedError

    def saver(self, resource: Any) -> Any:
        raise NotImplementedError

    def value_encoding(self, saver: Any, compress_threshold: int | None) -> Any:
        raise NotImplementedError

    async def recorded_generation(self, resource: Any) -> int | None:
        raise NotImplementedError

    async def record_generation(self, resource: Any, generation: int) -> None:
        raise NotImplementedError

    async def holds_checkpoints(self, resource: Any) -> bool:
        raise NotImplementedError


def _facts(**overrides: Any) -> CheckpointProviderFacts:
    values: dict[str, Any] = {"durable": True, "retention": "sweep", "channel_storage": "per_version"}
    values.update(overrides)
    return CheckpointProviderFacts(**values)


# --------------------------------------------------------------------------- #
# The built-in stores' facts
# --------------------------------------------------------------------------- #
def test_each_built_in_store_states_its_three_facts():
    providers = checkpoint_providers()
    assert providers["redis"] == CheckpointProviderFacts(
        durable=True, retention="native_ttl", channel_storage="per_checkpoint"
    )
    assert providers["postgres"] == CheckpointProviderFacts(
        durable=True, retention="sweep", channel_storage="per_version"
    )
    assert providers["sqlite"] == CheckpointProviderFacts(
        durable=False, retention="sweep", channel_storage="per_checkpoint"
    )
    assert providers["memory"] == CheckpointProviderFacts(
        durable=False, retention="process", channel_storage="per_version"
    )


def test_the_provider_view_is_read_only_and_sorted_by_name():
    providers = checkpoint_providers()
    with pytest.raises(TypeError):
        providers["other"] = providers["memory"]  # type: ignore[index]
    assert list(providers) == sorted(providers)


def test_facts_of_a_known_provider():
    assert checkpoint_provider_facts("redis") is checkpoint_providers()["redis"]
    assert checkpoint_provider("redis").facts is checkpoint_provider_facts("redis")


def test_an_unknown_provider_is_refused_with_the_registered_list():
    with pytest.raises(UnknownCheckpointProviderError) as excinfo:
        checkpoint_provider("cassandra")
    known = ", ".join(checkpoint_providers())
    assert str(excinfo.value) == f"Unsupported checkpoint provider: 'cassandra'; known: {known}"
    assert isinstance(excinfo.value, ValueError)
    with pytest.raises(UnknownCheckpointProviderError):
        checkpoint_provider_facts("cassandra")


def test_durable_providers():
    durable = durable_checkpoint_providers()
    assert {"redis", "postgres"} <= durable
    assert not {"sqlite", "memory"} & durable
    assert durable == frozenset(name for name, facts in checkpoint_providers().items() if facts.durable)


# --------------------------------------------------------------------------- #
# A facts row refuses what it cannot mean
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"retention": "forever"}, "retention must be one of native_ttl, sweep, process, got 'forever'"),
        ({"channel_storage": "per_page"}, "channel_storage must be one of per_checkpoint, per_version, got 'per_page'"),
        ({"durable": "yes"}, "durable must be a bool, got 'yes'"),
        ({"retention": "process"}, "a checkpoint store whose threads end with the process is not durable"),
    ],
)
def test_a_facts_row_outside_its_meaning_is_refused(overrides, message):
    with pytest.raises(ValueError, match=message):
        _facts(**overrides)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_a_registered_store_is_selected_by_its_name(registry):
    @register_checkpoint_provider
    class Selected(_Hooks):
        name: ClassVar[str] = "selected-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts()

    assert isinstance(checkpoint_provider("selected-store"), Selected)
    assert checkpoint_provider("selected-store") is checkpoint_provider("selected-store")
    assert checkpoint_providers()["selected-store"] == _facts()
    assert "selected-store" in durable_checkpoint_providers()


def test_the_same_class_registered_again_changes_nothing(registry):
    class Again(_Hooks):
        name: ClassVar[str] = "again-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts()

    register_checkpoint_provider(Again)
    first = checkpoint_provider("again-store")
    register_checkpoint_provider(Again)
    assert checkpoint_provider("again-store") is first


def test_another_class_under_a_held_name_is_refused(registry):
    class Holder(_Hooks):
        name: ClassVar[str] = "held-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts()

    class Intruder(_Hooks):
        name: ClassVar[str] = "held-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts(durable=False)

    register_checkpoint_provider(Holder)
    with pytest.raises(ValueError, match="Checkpoint provider 'held-store' already registered"):
        register_checkpoint_provider(Intruder)
    assert isinstance(checkpoint_provider("held-store"), Holder)


def test_a_built_in_name_cannot_be_taken(registry):
    class Imposter(_Hooks):
        name: ClassVar[str] = "redis"
        facts: ClassVar[CheckpointProviderFacts] = _facts()

    with pytest.raises(ValueError, match="Checkpoint provider 'redis' already registered"):
        register_checkpoint_provider(Imposter)


@pytest.mark.parametrize("name", ["", "Upper", "9lives", "has space", "dot.ted", None])
def test_a_malformed_name_is_refused(registry, name):
    class Named(_Hooks):
        facts: ClassVar[CheckpointProviderFacts] = _facts()

    if name is not None:
        Named.name = name
    with pytest.raises(ValueError, match="must match"):
        register_checkpoint_provider(Named)


def test_facts_that_are_not_a_facts_row_are_refused(registry):
    class Loose(_Hooks):
        name: ClassVar[str] = "loose-store"
        facts: ClassVar[Any] = {"durable": True, "retention": "sweep", "channel_storage": "per_version"}

    with pytest.raises(TypeError, match=r"'loose-store' declares facts .* not CheckpointProviderFacts"):
        register_checkpoint_provider(Loose)


def test_a_store_kept_by_its_keys_ttl_needs_a_saver_with_its_own_waiting(registry):
    class Keyed(_Hooks):
        name: ClassVar[str] = "keyed-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts(retention="native_ttl")

    with pytest.raises(
        ValueError, match=r"'keyed-store' keeps threads by its keys' TTL .* defines no saver_with_waiting"
    ):
        register_checkpoint_provider(Keyed)

    class KeyedWithWaiting(Keyed):
        async def saver_with_waiting(self, resource: Any, waiting_minutes: int) -> Any:
            raise NotImplementedError

    register_checkpoint_provider(KeyedWithWaiting)
    assert checkpoint_provider_facts("keyed-store").retention == "native_ttl"


def test_a_store_missing_a_hook_is_refused_naming_it(registry):
    class Partial(CheckpointProvider):
        name: ClassVar[str] = "partial-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts()

        async def open(self, conn_string: str | None) -> Any:
            raise NotImplementedError

    with pytest.raises(TypeError, match="Partial") as excinfo:
        register_checkpoint_provider(Partial)
    assert "saver" in str(excinfo.value)
    with pytest.raises(UnknownCheckpointProviderError):
        checkpoint_provider("partial-store")


def test_only_a_provider_class_registers(registry):
    with pytest.raises(TypeError, match="CheckpointProvider subclass"):
        register_checkpoint_provider(object)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The park horizon reads the facts
# --------------------------------------------------------------------------- #
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


def test_a_registered_durable_store_has_a_park_horizon(registry, monkeypatch):
    @register_checkpoint_provider
    class Durable(_Hooks):
        name: ClassVar[str] = "durable-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts()

    monkeypatch.setattr(
        providers_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(checkpoint_retention_waiting_minutes=60),
    )
    assert checkpoint_park_horizon("durable-store") == timedelta(minutes=60)


# --------------------------------------------------------------------------- #
# shared_value_threshold: the default from the channel storage, the deployment's switch, the size
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
    ("provider", "expected"), [("redis", 65536), ("sqlite", 65536), ("postgres", None), ("memory", None)]
)
def test_unset_shares_where_the_store_copies_values_into_every_checkpoint(switch, provider, expected):
    switch(None)
    assert shared_value_threshold(provider) == expected


@pytest.mark.parametrize("provider", ["redis", "postgres", "sqlite", "memory"])
def test_the_switch_off_stores_every_value_inline_on_every_store(switch, provider):
    switch("false")
    assert shared_value_threshold(provider) is None


@pytest.mark.parametrize("provider", ["redis", "postgres", "sqlite", "memory"])
def test_the_switch_on_stores_values_once_on_every_store(switch, provider):
    switch("true", size="2048")
    assert shared_value_threshold(provider) == 2048


def test_a_registered_store_takes_its_default_from_its_channel_storage(switch, registry):
    class Copying(_Hooks):
        name: ClassVar[str] = "copying-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts(channel_storage="per_checkpoint")

    class Versioned(_Hooks):
        name: ClassVar[str] = "versioned-store"
        facts: ClassVar[CheckpointProviderFacts] = _facts(channel_storage="per_version")

    register_checkpoint_provider(Copying)
    register_checkpoint_provider(Versioned)
    switch(None)
    assert shared_value_threshold("copying-store") == 65536
    assert shared_value_threshold("versioned-store") is None
    switch("true")
    assert shared_value_threshold("versioned-store") == 65536
    switch("false")
    assert shared_value_threshold("copying-store") is None


def test_shared_value_threshold_refuses_an_unknown_provider(switch):
    switch(None)
    with pytest.raises(UnknownCheckpointProviderError):
        shared_value_threshold("cassandra")
