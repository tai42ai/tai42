"""The conversations manager serves its stores, and the conversations/interactions settings are read once per epoch."""

from __future__ import annotations

import logging

import pytest
from tai42_kit.clients.base import advance_client_epoch, current_client_epoch
from tai42_kit.settings import reset_all_settings, sweep_stale_settings

from tai42_skeleton.app.retired_generations import certify_retired_generation
from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations.ledger import ChannelSendLedger
from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
from tai42_skeleton.conversations.managers.redis_conversations_manager import RedisConversationsManager
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore
from tai42_skeleton.conversations.mode import ConversationModeStore
from tai42_skeleton.conversations.pair_codes import ConversationPairCodeStore
from tai42_skeleton.conversations.persons import ConversationPersonStore
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.redeem_throttle import ConversationRedeemThrottle
from tai42_skeleton.conversations.settings import ConversationsSettings, conversations_settings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore
from tai42_skeleton.interactions import settings as interactions_settings_module
from tai42_skeleton.interactions.settings import interactions_store_configured
from tai42_skeleton.operations.errors import NotSupportedError

_ACCESSORS = {
    "records": ConversationRecordStore,
    "persons": ConversationPersonStore,
    "modes": ConversationModeStore,
    "pair_codes": ConversationPairCodeStore,
    "target_configs": ConversationTargetConfigStore,
    "redeem_throttle": ConversationRedeemThrottle,
    "media_meta": InboundMediaMetaStore,
    "send_ledger": ChannelSendLedger,
}


@pytest.fixture
def durable_env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


@pytest.fixture
def in_memory_env(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)


def _count_inits(monkeypatch, cls) -> list[None]:
    """Record one entry per ``cls`` construction (exact type only, so nested groups do not count)."""
    calls: list[None] = []
    original = cls.__init__

    def counting_init(self, *args, **kwargs):
        if type(self) is cls:
            calls.append(None)
        original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "__init__", counting_init)
    return calls


def test_the_redis_manager_is_durable(durable_env):
    manager = cache_module.get_conversations_manager()
    assert isinstance(manager, RedisConversationsManager)
    assert manager.durable is True


def test_the_in_memory_manager_is_not_durable(in_memory_env):
    manager = cache_module.get_conversations_manager()
    assert isinstance(manager, InMemoryConversationsManager)
    assert manager.durable is False


@pytest.mark.parametrize("name", sorted(_ACCESSORS))
def test_each_accessor_on_the_in_memory_manager_refuses_every_time(in_memory_env, name):
    manager = cache_module.get_conversations_manager()
    with pytest.raises(NotSupportedError):
        getattr(manager, name)
    # A refusal is not cached: the next read refuses again, never a stale half-built value.
    with pytest.raises(NotSupportedError):
        getattr(manager, name)


@pytest.mark.parametrize(("name", "store_cls"), sorted(_ACCESSORS.items()))
def test_each_accessor_serves_one_store_over_the_manager_settings(durable_env, name, store_cls):
    manager = cache_module.get_conversations_manager()
    store = getattr(manager, name)
    assert type(store) is store_cls
    assert store.settings is manager.settings
    assert getattr(manager, name) is store


def test_the_accessors_are_rebuilt_after_a_settings_reset(durable_env):
    manager = cache_module.get_conversations_manager()
    stores = {name: getattr(manager, name) for name in _ACCESSORS}
    reset_all_settings()
    rebuilt = cache_module.get_conversations_manager()
    assert rebuilt is not manager
    assert rebuilt.settings is not manager.settings
    for name, store in stores.items():
        assert getattr(rebuilt, name) is not store
        assert getattr(rebuilt, name).settings is rebuilt.settings


def test_the_conversations_settings_are_built_once_per_epoch(durable_env, monkeypatch):
    builds = _count_inits(monkeypatch, ConversationsSettings)
    first = conversations_settings()
    manager = cache_module.get_conversations_manager()
    for name in _ACCESSORS:
        getattr(manager, name)
    assert conversations_settings() is first
    assert manager.settings is first
    assert len(builds) == 1


def test_a_settings_reset_re_reads_the_conversations_settings(durable_env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_PREFIX", "first")
    assert conversations_settings().prefix == "first"
    assert cache_module.get_conversations_manager().records.settings.prefix == "first"
    builds = _count_inits(monkeypatch, ConversationsSettings)
    monkeypatch.setenv("CONVERSATIONS_PREFIX", "second")
    # Inside one epoch the snapshot holds.
    assert conversations_settings().prefix == "first"
    reset_all_settings()
    assert conversations_settings().prefix == "second"
    assert cache_module.get_conversations_manager().records.settings.prefix == "second"
    assert len(builds) == 1


def test_a_reset_reselects_the_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    assert cache_module.get_conversations_manager().durable is False
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    reset_all_settings()
    assert cache_module.get_conversations_manager().durable is True


@pytest.mark.parametrize("env_name", ["INTERACTIONS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"])
def test_interactions_store_configured_follows_its_url_across_a_reset(monkeypatch, env_name):
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    assert interactions_store_configured() is False
    monkeypatch.setenv(env_name, "redis://localhost:1/0")
    # Inside one epoch the cached settings hold.
    assert interactions_store_configured() is False
    reset_all_settings()
    assert interactions_store_configured() is True
    monkeypatch.delenv(env_name)
    reset_all_settings()
    assert interactions_store_configured() is False


def test_interactions_store_configured_reads_the_cached_settings(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:1/0")
    builds = _count_inits(monkeypatch, interactions_settings_module.InteractionsRedisSettings)
    for _ in range(5):
        assert interactions_store_configured() is True
    assert len(builds) == 1


@pytest.mark.parametrize("backend_env", ["in_memory_env", "durable_env"])
def test_a_retired_settings_generation_is_not_held_by_the_manager(request, caplog, backend_env):
    # A reload retires the manager with its settings; the retire sweep must find no
    # retired-generation ConversationsSettings still reachable through it.
    request.getfixturevalue(backend_env)
    reset_all_settings()
    manager = cache_module.get_conversations_manager()
    retired = current_client_epoch()
    del manager
    advance_client_epoch()

    with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
        reset_all_settings()
        certify_retired_generation(retired)

    held = [h for h in sweep_stale_settings(retired) if h.settings_type.endswith(".ConversationsSettings")]
    assert held == []
    assert "ConversationsSettings" not in caplog.text
    # The next read serves a manager built over the new generation's settings.
    assert cache_module.get_conversations_manager().settings is conversations_settings()
