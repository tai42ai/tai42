"""The values of the worker-bus settings that locate the fleet a process is a member of.

The Redis URL and the namespace locate the census; the heartbeat, the two timeouts and the
connection's behaviour knobs are how a member takes part, not where the fleet is. Every bus
key and channel is under the namespace and the running skeleton's major, so workers of two
majors on one Redis are two fleets.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from pathlib import Path

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.bus import WorkerBus, WorkerKind
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings, fleet_env_names

from .._helpers import another_skeleton_major


def _settings(
    fields: Mapping[str, object] | None = None, *, redis_url: str | None = "redis://127.0.0.1:6379/0"
) -> BusSettings:
    return BusSettings.model_validate({"redis": BusRedisSettings(redis_url=redis_url), **(fields or {})})


@pytest.mark.parametrize(
    "fields", [{"heartbeat_ttl": 21.0}, {"ack_timeout": 3.0}, {"apply_timeout": 120.0}], ids=lambda f: next(iter(f))
)
def test_settings_that_differ_only_in_how_a_member_takes_part_locate_the_same_fleet(fields: dict[str, float]) -> None:
    assert _settings(fields).fleet == _settings().fleet


def test_a_different_socket_timeout_locates_the_same_fleet() -> None:
    timed = BusSettings(redis=BusRedisSettings(redis_url="redis://127.0.0.1:6379/0", socket_timeout=9.0))

    assert timed.fleet == _settings().fleet


@pytest.mark.parametrize("redis_url", ["redis://127.0.0.1:6380/0", None])
def test_settings_that_differ_in_the_redis_url_locate_another_fleet(redis_url: str | None) -> None:
    assert _settings(redis_url=redis_url).fleet != _settings().fleet


def test_settings_that_differ_in_the_namespace_locate_another_fleet() -> None:
    assert _settings({"namespace": "other"}).fleet != _settings().fleet


def test_the_local_bus_is_on_the_busless_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL", "TAI_BUS_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)
    reset_all_settings()

    assert WorkerBus.local(WorkerKind.serve).settings.fleet == _settings(redis_url=None).fleet


def test_a_bus_carries_the_settings_it_was_built_under() -> None:
    settings = _settings({"heartbeat_ttl": 21.0})

    assert WorkerBus(settings, kind=WorkerKind.serve).settings is settings


def test_the_fleet_env_names_are_every_name_read_for_the_redis_url_and_the_namespace() -> None:
    assert fleet_env_names() == frozenset(
        {"TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL", "TAI_BUS_REDIS", "TAI_BUS_NAMESPACE"}
    )


def test_a_process_in_its_config_dir_reads_no_bus_setting_from_the_stored_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text('TAI_BUS_REDIS_URL="redis://127.0.0.1:1/0"\nTAI_BUS_HEARTBEAT_TTL="21"\n')
    for name in ("TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL", "TAI_BUS_HEARTBEAT_TTL", "TAI_CONFIG_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAI_CONFIG_DIR_PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    settings = BusSettings()

    assert (settings.redis.redis_url, settings.heartbeat_ttl) == (None, 15.0)


def _derived_names(settings: BusSettings) -> list[str]:
    return [
        settings.channel,
        settings.reply_prefix,
        settings.presence_prefix,
        settings.presence_index,
        settings.presence_key("serve-1"),
        settings.slot_key("serve-1"),
        settings.gen_key("serve-1"),
        settings.env_readers_key("digest"),
    ]


def test_every_bus_key_is_under_the_namespace_and_the_running_skeleton_major() -> None:
    major = int(importlib.metadata.version("tai42-skeleton").split(".", 1)[0])
    settings = _settings({"namespace": "stack"})

    assert [name for name in _derived_names(settings) if not name.startswith(f"stack:v{major}:")] == []


def test_workers_of_two_majors_on_one_namespace_share_no_bus_key(monkeypatch: pytest.MonkeyPatch) -> None:
    running = _settings({"namespace": "stack"})
    with another_skeleton_major(monkeypatch) as other:
        foreign = _settings({"namespace": "stack"})

    assert foreign.presence_index.startswith(f"stack:v{other}:")
    assert set(_derived_names(running)) & set(_derived_names(foreign)) == set()
