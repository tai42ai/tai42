"""Every env writer refuses a change to a key that locates the worker bus, on every door.

The bus URLs and ``TAI_BUS_NAMESPACE`` decide which bus, and which census on it, a worker
joins; the recycle orchestrator's census rides that bus, so no recycle can carry a change to
one. The merge write, the combined env + manifest write, the profile apply and the profile
diff each refuse it, naming the key, before anything is persisted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tai42_skeleton.app.bus_settings import fleet_env_names
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.config.deployment_env import DeploymentOwnership

from .._helpers import recapture_deployment_env
from .fake_pipeline import (
    FakeConfigStore,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
)

_BUS_LOCATING = {
    "TAI_BUS_REDIS_URL": "redis://bus.invalid/1",
    "TAI_DEFAULT_REDIS_URL": "redis://bus.invalid/2",
    "TAI_BUS_NAMESPACE": "other-fleet",
    # The bus Redis group read as one JSON value under its own name.
    "TAI_BUS_REDIS": '{"redis_url": "redis://bus.invalid/3"}',
    # The settings layer reads env names case-insensitively.
    "tai_bus_namespace": "lower-fleet",
}


@pytest.fixture(autouse=True)
def _no_deployment_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _BUS_LOCATING:
        monkeypatch.delenv(key, raising=False)
    recapture_deployment_env()


def test_the_cases_are_every_name_the_settings_layer_reads_for_the_bus_location() -> None:
    assert frozenset(key.upper() for key in _BUS_LOCATING) == fleet_env_names()


@pytest.mark.parametrize(("key", "value"), sorted(_BUS_LOCATING.items()))
async def test_the_merge_write_refuses_it(key: str, value: str) -> None:
    store = FakeConfigStore(env={})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match=f"bus-reaching key\\(s\\): {key}\\."):
        await service.apply_env_change({key: value})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("key", "value"), sorted(_BUS_LOCATING.items()))
async def test_the_env_and_manifest_write_refuses_it(key: str, value: str) -> None:
    store = FakeConfigStore(env={}, manifest={})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return {key: value}, add_server

    with pytest.raises(ValueError, match=f"bus-reaching key\\(s\\): {key}\\."):
        await service.apply_env_and_change(prepare)

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("key", "value"), sorted(_BUS_LOCATING.items()))
async def test_the_profile_apply_refuses_it(key: str, value: str) -> None:
    store = FakeConfigStore(env={})
    service, _admin, _bus = _service(store)
    swapped: list[dict[str, str]] = []
    saved: list[dict[str, str]] = []

    async def build_and_swap(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        swapped.append(dict(env))
        return Epoch(number=0)

    async def save_previous(stored_env: dict[str, str]) -> None:
        saved.append(dict(stored_env))

    swap: Callable[..., Awaitable[Epoch]] = build_and_swap
    with pytest.raises(ValueError, match=f"bus-reaching key\\(s\\): {key}\\."):
        await service.apply_replace_env({key: value}, save_previous=save_previous, build_and_swap=swap)

    assert store.env_writes == []
    assert saved == []
    assert swapped == []


def test_the_refusal_names_what_the_key_reaches() -> None:
    from tai42_skeleton.config.recycle_policy import _refuse_unrecyclable

    with pytest.raises(ValueError, match="TAI_BUS_NAMESPACE") as excinfo:
        _refuse_unrecyclable({"TAI_BUS_NAMESPACE"}, change="this env change")

    message = str(excinfo.value)
    assert "which no recycle can carry" in message
    assert "deployment's environment" in message
