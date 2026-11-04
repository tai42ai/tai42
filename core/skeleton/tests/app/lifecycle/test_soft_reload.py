"""The soft config reload: the deployment ownership it applies by, the bus slot it keeps across a long build, and
the recycle-class keys it states."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import fakeredis
import pytest
from tai42_contract.errors import ClientDisconnectedError
from tai42_kit.settings import bind_respawn_class_env

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app import applied_env
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.bus import SlotLostError, WorkerBus, WorkerKind
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings
from tai42_skeleton.app.discarded_build import DISCARDED_BUILD_STATUS, discarded_build_statement, mark_discarded_build
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.deployment_env import DeploymentOwnership, fleet_deployment_ownership

from ..._fakes.bus import FakeBus
from ..._fakes.kit_redis import KitFakeRedis
from ._doubles import _EnvConfig, _Mixin


class _OutageBus(FakeBus):
    """A bus that answers until ``down`` is set, then fails its census the way a severed bus does."""

    down = False

    async def census(self) -> list[Any]:
        if self.down:
            raise ClientDisconnectedError("bus down")
        return await super().census()


async def test_a_reload_while_the_bus_cannot_answer_applies_by_the_names_last_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    owned: list[bool] = []

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Any:
        owned.append(ownership.owns("RELOAD_TEST_SIBLING_ONLY"))

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    bus = _OutageBus(origin="serve-1", remotes=["backend-1"])
    bus.rows[1].deployment_keys = ["RELOAD_TEST_SIBLING_ONLY"]
    await fleet_deployment_ownership(bus)
    bus.down = True
    mixin = _Mixin()
    mixin._bus = cast("Any", bus)
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin.bind_serving_loop()

    with caplog.at_level(logging.WARNING):
        result = await mixin._reload_config()

    assert result["status"] == "ok"
    assert owned == [True]
    assert any("'reload_config'" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_a_gated_reload_that_blocks_the_serving_loop_keeps_the_bus_slot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A config reload driven through the gate marshals its build onto the serving loop,
    # whose thread the build blocks for longer than the bus claim TTL. The worker-bus
    # subscription on that same loop keeps its slot: same name and generation, one resync.
    server = fakeredis.FakeServer()

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs) -> AsyncIterator[KitFakeRedis]:
        client = KitFakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)
    ttl = 0.6

    async def _blocking_build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Any:
        time.sleep(ttl * 2.5)

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _blocking_build)
    mixin = _Mixin()
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin.bind_serving_loop()

    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), namespace="reload", heartbeat_ttl=ttl),
        kind=WorkerKind.serve,
        exit_drain=lambda: None,
        reconnect_backoff_initial=0.02,
        reconnect_backoff_max=0.05,
    )
    mixin._bus = bus
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
        # Stopped as its process's stop does: the life outlives the subscription until it
        # leaves the census, which stops and joins its heartbeat thread.
        subscription.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscription
        await bus.leave_census()


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

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Any:
        applied_env.apply_env_and_reset_settings(env, ownership)

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    monkeypatch.setattr(applied_env, "_loaded_env_keys", set())
    mixin = _Mixin()
    mixin._bus = cast("Any", FakeBus(origin="serve-1"))
    mixin._config_manager = _RecycleEnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin.bind_serving_loop()

    with caplog.at_level(logging.ERROR):
        result = await mixin._reload_config()

    # The reload states the fact; the operation that started it judges whether anything carries it.
    assert result == {"status": "ok", "env_keys": 2, "awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]}
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == []


def _refusing_mixin(monkeypatch: pytest.MonkeyPatch, refusal: Exception) -> _Mixin:
    """A lifecycle whose epoch build raises ``refusal`` with the env left as it stood before the build."""

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Any:
        raise refusal

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    mixin = _Mixin()
    mixin._bus = cast("Any", FakeBus(origin="serve-1"))
    mixin._config_manager = _RecycleEnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin.bind_serving_loop()
    return mixin


async def test_a_reload_refused_at_its_epoch_build_states_the_drift_left_standing_after_the_discard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Imported for its registration side effect: the bus group is recycle-class.
    import tai42_skeleton.app.bus_settings  # noqa: F401

    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "15")
    bind_respawn_class_env()
    # An earlier change moved the key and no restart followed it: the drift stands.
    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "9")
    refusal = RuntimeError("plugin migration chain pending")
    mark_discarded_build(refusal)
    mixin = _refusing_mixin(monkeypatch, refusal)

    with pytest.raises(RuntimeError) as raised:
        await mixin._reload_config()

    assert raised.value is refusal
    assert discarded_build_statement(refusal) == {
        "status": DISCARDED_BUILD_STATUS,
        "awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"],
    }


async def test_a_reload_failing_any_other_way_states_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = RuntimeError("reload boom")
    mixin = _refusing_mixin(monkeypatch, failure)

    with pytest.raises(RuntimeError) as raised:
        await mixin._reload_config()

    assert raised.value is failure
    assert discarded_build_statement(failure) is None
