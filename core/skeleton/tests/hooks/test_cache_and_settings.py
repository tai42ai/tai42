"""The manager-selection singleton (in-memory vs redis by config) and the
``HooksSettings`` derived keys / ``in_memory`` flag.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError
from tai42_contract.hooks.models import HookParams
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.hooks.cache import get_hooks_manager
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.runs.models import HookRun
from tai42_skeleton.hooks.settings import HooksRedisSettings, HooksSettings


def _clear_singleton() -> None:
    get_hooks_manager.cache_clear()


def test_selects_in_memory_when_no_redis_url(monkeypatch):
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    _clear_singleton()
    try:
        assert isinstance(get_hooks_manager(), InMemoryHooksManager)
    finally:
        _clear_singleton()


def test_selects_redis_when_redis_url_set(monkeypatch):
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    _clear_singleton()
    try:
        assert isinstance(get_hooks_manager(), RedisHooksManager)
    finally:
        _clear_singleton()


def test_in_memory_selection_warns_loudly(monkeypatch, caplog):
    # In-memory mode silently no-ops sibling deliveries under multi-worker serving,
    # so its selection emits a loud once-per-process warning.
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    _clear_singleton()
    try:
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.hooks.cache"):
            manager = get_hooks_manager()
        assert isinstance(manager, InMemoryHooksManager)
        assert "IN-MEMORY" in caplog.text
        assert "HOOKS_REDIS_URL" in caplog.text
    finally:
        _clear_singleton()


def test_redis_selection_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    _clear_singleton()
    try:
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.hooks.cache"):
            manager = get_hooks_manager()
        assert isinstance(manager, RedisHooksManager)
        assert "IN-MEMORY" not in caplog.text
    finally:
        _clear_singleton()


def test_reload_drops_the_hooks_manager_singleton(monkeypatch):
    # The singleton is registered with the settings-reset registry, so a live
    # reload rebuilds it against the new config (backend + connection) rather than
    # keeping the stale one bound to the old HooksSettings.
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    _clear_singleton()
    try:
        first = get_hooks_manager()
        assert isinstance(first, InMemoryHooksManager)

        # Flip the backend config and reload every registered settings cache.
        monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
        reset_all_settings()

        rebuilt = get_hooks_manager()
        assert rebuilt is not first
        assert isinstance(rebuilt, RedisHooksManager)
    finally:
        _clear_singleton()


async def test_hook_count_reflects_live_in_memory_hooks():
    manager = InMemoryHooksManager(HooksSettings())
    assert manager.hook_count == 0
    await manager.register(
        HookParams(
            name="a", topic="events", tool="forward", execution_key="k-fire", execution_key_fingerprint="fp-fire"
        )
    )
    await manager.register(
        HookParams(name="b", topic="events", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )
    await manager.register(
        HookParams(
            name="c", topic="signals", tool="process", execution_key="k-fire", execution_key_fingerprint="fp-fire"
        )
    )
    # Counts across every topic bucket.
    assert manager.hook_count == 3
    await manager.unregister("b")
    assert manager.hook_count == 2


async def test_reset_warns_naming_count_and_drops_in_memory_hooks(monkeypatch, caplog):
    # The in-memory manager honors HOOKS_* config, so a reload drops it — and its
    # in-memory hooks — with a loud warning naming the count (not a silent loss).
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    _clear_singleton()
    try:
        manager = get_hooks_manager()
        assert isinstance(manager, InMemoryHooksManager)
        await manager.register(
            HookParams(
                name="h1", topic="events", tool="forward", execution_key="k-fire", execution_key_fingerprint="fp-fire"
            )
        )
        assert manager.hook_count == 1

        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.hooks.cache"):
            reset_all_settings()

        assert "discarding the in-memory hooks manager with 1 registered hook(s)" in caplog.text

        rebuilt = get_hooks_manager()
        assert rebuilt is not manager
        assert isinstance(rebuilt, InMemoryHooksManager)
        assert rebuilt.hook_count == 0
    finally:
        _clear_singleton()


async def test_reset_empty_in_memory_manager_emits_no_warning(monkeypatch, caplog):
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    _clear_singleton()
    try:
        assert isinstance(get_hooks_manager(), InMemoryHooksManager)  # no hooks registered
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.hooks.cache"):
            reset_all_settings()
        assert "discarding the in-memory hooks manager" not in caplog.text
    finally:
        _clear_singleton()


def test_reset_redis_mode_emits_no_warning(monkeypatch, caplog):
    # A redis-backed manager keeps its state in Redis, so a reset loses nothing and
    # must not warn.
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    _clear_singleton()
    try:
        assert isinstance(get_hooks_manager(), RedisHooksManager)
        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.hooks.cache"):
            reset_all_settings()
        assert "discarding the in-memory hooks manager" not in caplog.text
    finally:
        _clear_singleton()


def test_in_memory_flag_reflects_redis_url():
    assert HooksSettings().in_memory is True
    with_url = HooksSettings(redis=HooksRedisSettings(redis_url="redis://localhost:6379/0"))
    assert with_url.in_memory is False


def test_derived_keys():
    settings = HooksSettings()
    assert settings.get_hook_key("events") == "hooks:topic:events"
    assert settings.name_trigger_map_key == "hooks:name_trigger_map"


def test_trigger_key_helpers_round_trip():
    settings = HooksSettings()
    token_hash = "a" * 64

    # Each helper builds its documented ``hooks:trigger:rec:/name:/tomb:`` form.
    assert settings.trigger_record_key(token_hash) == f"hooks:trigger:rec:{token_hash}"
    assert settings.trigger_name_key("events") == "hooks:trigger:name:events"
    assert settings.trigger_tomb_key(token_hash) == f"hooks:trigger:tomb:{token_hash}"

    # The prefix properties match exactly what the key helpers prepend.
    assert settings.trigger_record_key_prefix == "hooks:trigger:rec:"
    assert settings.trigger_name_key_prefix == "hooks:trigger:name:"
    assert settings.trigger_tomb_key_prefix == "hooks:trigger:tomb:"
    assert settings.trigger_record_key(token_hash) == f"{settings.trigger_record_key_prefix}{token_hash}"
    assert settings.trigger_name_key("events") == f"{settings.trigger_name_key_prefix}events"
    assert settings.trigger_tomb_key(token_hash) == f"{settings.trigger_tomb_key_prefix}{token_hash}"

    # The scan patterns glob their own prefixes.
    assert settings.trigger_name_scan_pattern() == "hooks:trigger:name:*"
    assert settings.trigger_tomb_scan_pattern() == "hooks:trigger:tomb:*"


def test_max_workers_defaults_and_rejects_non_positive():
    # The global in-flight bound is always on: it defaults to 10 and a
    # non-positive value is a config error, never an unbounded mode.
    assert HooksSettings().max_workers == 10
    with pytest.raises(ValidationError):
        HooksSettings(max_workers=0)
    with pytest.raises(ValidationError):
        HooksSettings(max_workers=-1)


def test_hook_run_settings_defaults():
    settings = HooksSettings()
    assert settings.run_max_attempts == 5
    assert settings.run_retry_base_seconds == 10.0
    assert settings.run_retry_cap_seconds == 600.0
    assert settings.run_lease_seconds == 60.0
    assert settings.run_sweep_seconds == 5.0
    assert settings.run_shutdown_grace_seconds == 20.0


def test_hook_run_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("HOOKS_RUN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("HOOKS_RUN_RETRY_BASE_SECONDS", "2")
    monkeypatch.setenv("HOOKS_RUN_SHUTDOWN_GRACE_SECONDS", "0")
    settings = HooksSettings()
    assert (settings.run_max_attempts, settings.run_retry_base_seconds) == (3, 2.0)
    assert settings.run_shutdown_grace_seconds == 0.0


def test_a_retry_base_above_its_cap_is_rejected():
    with pytest.raises(
        ValidationError, match="HOOKS_RUN_RETRY_BASE_SECONDS must not exceed HOOKS_RUN_RETRY_CAP_SECONDS"
    ):
        HooksSettings(run_retry_base_seconds=601, run_retry_cap_seconds=600)
    assert HooksSettings(run_retry_base_seconds=600, run_retry_cap_seconds=600).run_retry_base_seconds == 600


@pytest.mark.parametrize(
    "field",
    ["run_max_attempts", "run_retry_base_seconds", "run_retry_cap_seconds", "run_lease_seconds", "run_sweep_seconds"],
)
def test_hook_run_settings_reject_non_positive(field):
    with pytest.raises(ValidationError):
        HooksSettings.model_validate({field: 0})


def test_a_negative_shutdown_grace_is_rejected():
    with pytest.raises(ValidationError):
        HooksSettings(run_shutdown_grace_seconds=-1)


def _job(run_id: str) -> HookRun:
    return HookRun(
        id=run_id,
        topic="events",
        hook="h1",
        payload={},
        status="pending",
        attempts=0,
        next_attempt_at=0.0,
        created_at=0.0,
    )


async def test_reset_warns_naming_the_hook_runs_it_drops(monkeypatch, caplog):
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    _clear_singleton()
    try:
        manager = get_hooks_manager()
        assert isinstance(manager, InMemoryHooksManager)
        await manager._accept_runs("events", [_job(f"r{i}") for i in range(6)], None, None, None)
        # Claim three (running), then fail one of them.
        claimed = await manager._claim_due_runs(1.0, 3, "me", 100.0, frozenset())
        await manager._fail_run(claimed[0].id, "me", "boom", "unknown", 1.0, "", (), None)

        with caplog.at_level(logging.WARNING, logger="tai42_skeleton.hooks.cache"):
            reset_all_settings()

        assert (
            "discarding the in-memory hooks manager with 0 registered hook(s) and 3 pending, 2 running and "
            "1 failed hook run(s)"
        ) in caplog.text
        assert get_hooks_manager() is not manager
    finally:
        _clear_singleton()
