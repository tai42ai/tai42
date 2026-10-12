"""The soft config reload releases every loop-bound kit registry before its build, on the serving loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from fakeredis import aioredis
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry
from tai42_kit.settings import bind_respawn_class_env

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.bus import SlotLostError, WorkerBus, WorkerKind
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings
from tai42_skeleton.app.reload_gate import reload_gate

from ._doubles import _Mixin


class _EnvConfig:
    def read_env(self) -> dict[str, str]:
        return {"A": "1"}


async def test_the_reload_closes_the_store_when_the_checkpoint_close_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def _failing_close() -> None:
        closed.append("checkpoint")
        raise RuntimeError("checkpoint close failed")

    async def _store_close() -> None:
        closed.append("store")

    async def _checkpoint_resource() -> tuple[Any, Any]:
        return object(), _failing_close

    async def _store_resource() -> tuple[Any, Any]:
        return object(), _store_close

    await checkpoint_registry()._get_or_init_resource("k", _checkpoint_resource)
    await store_registry()._get_or_init_resource("k", _store_resource)
    built: list[dict[str, str]] = []

    async def _build(env: dict[str, str]) -> Any:
        built.append(env)

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    mixin = _Mixin()
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin._serving_loop = asyncio.get_running_loop()

    with pytest.raises(ExceptionGroup, match="errors releasing loop-bound kit resources"):
        await asyncio.to_thread(mixin._reload_config)
    assert sorted(closed) == ["checkpoint", "store"]
    assert built == []


async def test_the_reload_releases_then_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def _release(**kwargs: Any) -> None:
        order.append("release")

    async def _build(env: dict[str, str]) -> Any:
        order.append("build")

    monkeypatch.setattr("tai42_skeleton.app.lifecycle.release_loop_bound_resources", _release)
    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    mixin = _Mixin()
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin._serving_loop = asyncio.get_running_loop()

    assert await asyncio.to_thread(mixin._reload_config) == {"status": "ok", "env_keys": 1, "awaiting_respawn": []}
    assert order == ["release", "build"]


async def test_a_gated_reload_that_blocks_the_serving_loop_keeps_the_bus_slot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A config reload driven through the gate marshals its build onto the serving loop,
    # whose thread the build blocks for longer than the bus claim TTL. The worker-bus
    # subscription on that same loop keeps its slot: same name and generation, one resync.
    server = fakeredis.FakeServer()

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs) -> AsyncIterator[aioredis.FakeRedis]:
        client = aioredis.FakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)
    ttl = 0.6

    async def _release(**kwargs: Any) -> None:
        return None

    async def _blocking_build(env: dict[str, str]) -> Any:
        time.sleep(ttl * 2.5)

    monkeypatch.setattr("tai42_skeleton.app.lifecycle.release_loop_bound_resources", _release)
    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _blocking_build)
    mixin = _Mixin()
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin._serving_loop = asyncio.get_running_loop()

    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), namespace="reload", heartbeat_ttl=ttl),
        kind=WorkerKind.serve,
        reconnect_backoff_initial=0.02,
        reconnect_backoff_max=0.05,
    )
    readies: list[str] = []

    async def _apply(_op: dict, *, deadline: float) -> None:
        return None

    async def _on_ready() -> None:
        readies.append("ready")

    subscription = asyncio.create_task(bus.subscribe(_apply, _on_ready))
    try:
        async with asyncio.timeout(3.0):
            while not readies:
                await asyncio.sleep(0.01)
        before = bus.identity
        with caplog.at_level(logging.WARNING):
            assert await reload_gate.run(mixin._reload_config, reimports=False) == {
                "status": "ok",
                "env_keys": 1,
                "awaiting_respawn": [],
            }
            # The heartbeat's next beat after the stall must still find the claim held.
            await asyncio.sleep(ttl)
        after = bus.identity
        assert (after.name, after.generation) == (before.name, before.generation)
        assert readies == ["ready"]
        assert not any(r.exc_info and isinstance(r.exc_info[1], SlotLostError) for r in caplog.records)
        assert not any("re-minting" in r.getMessage() for r in caplog.records)
    finally:
        subscription.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscription


class _RecycleEnvConfig:
    def read_env(self) -> dict[str, str]:
        return {"TAI_BUS_HEARTBEAT_TTL": "7", "LOG_LEVEL": "debug"}


async def test_the_reload_states_the_recycle_class_keys_moved_off_their_bound_value_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    # Imported for its registration side effect: the bus group is recycle-class.
    import tai42_skeleton.app.bus_settings  # noqa: F401

    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "15")
    monkeypatch.setenv("LOG_LEVEL", "info")
    bind_respawn_class_env()

    async def _build(env: dict[str, str]) -> Any:
        epoch_mod.apply_env_and_reset_settings(env)

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    monkeypatch.setattr(epoch_mod, "_loaded_env_keys", set())
    mixin = _Mixin()
    mixin._config_manager = _RecycleEnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin._serving_loop = asyncio.get_running_loop()

    with caplog.at_level(logging.ERROR):
        result = await asyncio.to_thread(mixin._reload_config)

    # The reload states the fact; the operation that started it judges whether anything carries it.
    assert result == {"status": "ok", "env_keys": 2, "awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]}
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == []
