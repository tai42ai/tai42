"""The values of the worker-bus settings that locate the fleet a process is a member of.

The Redis URL and the namespace locate the census; the heartbeat, the two timeouts and the
connection's behaviour knobs are how a member takes part, not where the fleet is.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.bus import WorkerBus, WorkerKind
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings, fleet_env_names


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
