"""settings_cache registry: cached accessors register for the global reset,
caches clear before custom hooks run, and hooks see fresh values."""

import pytest

from tai42_kit.settings import cache_registry
from tai42_kit.settings.cache_registry import (
    register_settings_reset,
    reset_all_settings,
    settings_cache,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Snapshot the global registry so test registrations don't leak."""
    monkeypatch.setattr(cache_registry, "_CACHE_CLEARS", dict(cache_registry._CACHE_CLEARS))
    monkeypatch.setattr(cache_registry, "_RESET_HOOKS", dict(cache_registry._RESET_HOOKS))


def test_settings_cache_caches_and_resets():
    calls = []

    @settings_cache
    def accessor() -> int:
        calls.append(1)
        return len(calls)

    assert accessor() == 1
    assert accessor() == 1
    reset_all_settings()
    assert accessor() == 2


def test_reset_clears_caches_before_hooks():
    order = []

    @settings_cache
    def accessor() -> str:
        order.append("accessor-built")
        return "value"

    accessor()
    order.clear()

    @register_settings_reset
    def hook() -> None:
        order.append("hook")
        accessor()

    reset_all_settings()
    assert order == ["hook", "accessor-built"]


def test_reregistration_is_idempotent():
    # A module re-import re-runs the decorator with a new function object of
    # the same qualified name — it must replace, not accumulate.
    calls = []

    def make():
        @register_settings_reset
        def hook() -> None:
            calls.append(1)

        return hook

    make()
    make()
    reset_all_settings()
    assert sum(calls) == 1


def test_hook_failure_propagates():
    @register_settings_reset
    def broken() -> None:
        raise RuntimeError("bad settings")

    with pytest.raises(RuntimeError, match="bad settings"):
        reset_all_settings()
