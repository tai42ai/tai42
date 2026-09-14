"""Trigger-link-local store fixtures: a redis-backed store over the shared fake, an
in-memory store, and a factory that installs an in-memory manager mid-test."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tai42_skeleton.hooks import trigger_links
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.settings import HooksSettings


@pytest.fixture
def store(monkeypatch, fake_redis, make_ctx):
    """A redis-backed trigger-link store over the fake: ``get_hooks_manager`` returns
    a ``RedisHooksManager`` (never in-memory) and both the module's and the manager's
    ``client_ctx`` yield the SAME fake, so verifier reads and store writes share it."""
    import tai42_skeleton.hooks.managers.redis_hooks_manager as rhm

    manager = RedisHooksManager(HooksSettings())
    monkeypatch.setattr(trigger_links, "get_hooks_manager", lambda: manager)
    monkeypatch.setattr(trigger_links, "client_ctx", make_ctx(fake_redis))
    monkeypatch.setattr(rhm, "client_ctx", make_ctx(fake_redis))
    return SimpleNamespace(manager=manager, redis=fake_redis, settings=manager.settings)


@pytest.fixture
def in_memory_store(monkeypatch):
    manager = InMemoryHooksManager(HooksSettings())
    monkeypatch.setattr(trigger_links, "get_hooks_manager", lambda: manager)
    return SimpleNamespace(manager=manager)


@pytest.fixture
def in_memory_store_factory(monkeypatch):
    def _install():
        manager = InMemoryHooksManager(HooksSettings())
        monkeypatch.setattr(trigger_links, "get_hooks_manager", lambda: manager)
        return manager

    return _install
