"""What a reload does to the loop-bound registries (checkpoint savers, stores).

A loop's current-epoch registries keep serving through every build, whichever loop drives it and
whether the build swaps, is discarded or fails; the retire closes the retired epoch's registries
on the serving loop, beside the generation's transports and client pools.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry
from tai42_kit.settings import reset_all_settings
from tests._fakes.rebuild import no_rebuild

from tai42_skeleton.app import applied_env, epoch_retire, instance
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.epoch import Epoch, build_and_discard_epoch
from tai42_skeleton.app.recycle import orchestrate_recycle
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.config.service import ConfigService

from .._fakes.bus import FakeBus
from ..config.fake_pipeline import ComboChecks, FakeConfigStore, FakeReloadAdmin
from ..connectors.credential_path import bind_as_serving, job_loop, owner_loop
from .lifecycle._doubles import _EnvConfig, _Mixin

# The door's edge settles the writer's exit its change owes; the apply here is driven below it.
pytestmark = pytest.mark.usefixtures("writer_exit_edge")


async def _cache_resources(closed: list[str], label: str) -> None:
    """Cache a checkpoint saver and a store on the running loop's current-epoch registries.

    Each resource's closer notes ``<label>-checkpoint`` / ``<label>-store`` in ``closed``.
    """

    def _resource(kind: str):
        async def _close() -> None:
            closed.append(f"{label}-{kind}")

        async def _factory() -> tuple[Any, Any]:
            return object(), _close

        return _factory

    await checkpoint_registry()._get_or_init_resource("saver", _resource("checkpoint"))
    await store_registry()._get_or_init_resource("store", _resource("store"))


async def _build_resetting_settings(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
    """A build that resets the settings as every epoch build does, and swaps nothing."""
    reset_all_settings()
    return Epoch(number=0)


class _PrevSpy:
    async def __call__(self, stored_env: dict[str, str]) -> None:
        return None


# ---------------------------------------------------------------------------
# The doors: a job loop holding a saver drives the build on the serving loop
# ---------------------------------------------------------------------------


async def test_an_apply_a_job_drives_after_its_loop_cached_a_saver_builds_on_the_serving_loop(
    serving: asyncio.AbstractEventLoop,
) -> None:
    store = FakeConfigStore(env={"MY_APP_FLAG": "old"})
    service = ConfigService(
        config_manager=store,
        admin=FakeReloadAdmin(),
        bus=cast("Any", FakeBus()),
        combo_check=ComboChecks(),
        orchestrate=orchestrate_recycle,
    )
    built_on: list[asyncio.AbstractEventLoop] = []
    closed: list[str] = []

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        built_on.append(asyncio.get_running_loop())
        return await _build_resetting_settings(env, ownership=ownership)

    async def _job() -> None:
        await _cache_resources(closed, "job")
        await service.apply_replace_env({"MY_APP_FLAG": "new"}, save_previous=_PrevSpy(), build_and_swap=_build)

    with job_loop() as job:
        await job.run_awaited(_job())
        assert closed == []
    assert built_on == [serving]
    assert store.env == {"MY_APP_FLAG": "new"}


async def test_a_reload_a_job_drives_after_its_loop_cached_a_saver_builds_on_the_serving_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built_on: list[asyncio.AbstractEventLoop] = []
    closed: list[str] = []

    async def _build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        built_on.append(asyncio.get_running_loop())
        return await _build_resetting_settings(env, ownership=ownership)

    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    mixin = _Mixin()
    mixin._bus = cast("Any", FakeBus(origin="serve-1"))
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(instance, "_app", mixin)

    async def _job() -> dict[str, Any]:
        await _cache_resources(closed, "job")
        return await reload_gate.run(mixin._reload_config, reimports=True)

    with owner_loop() as serving, job_loop() as job:
        bind_as_serving(mixin, serving)
        result = await job.run_awaited(_job())
        assert closed == []
    assert result == {"status": "ok", "env_keys": 1, "awaiting_respawn": []}
    assert built_on == [serving]


# ---------------------------------------------------------------------------
# A build that does not swap closes nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def _epoch_state() -> Iterator[None]:
    """Restore the epoch spine globals and the applied-env record a real build path touches."""
    loaded_before = set(applied_env._loaded_env_keys)
    try:
        yield
    finally:
        for name in ("_current", "_serving_slot"):
            setattr(epoch_mod, name, None)
        applied_env._loaded_env_keys = loaded_before


class _ProofBuildError(RuntimeError):
    pass


class _Serving:
    """A stand-in serving surface: a weakly referenceable object the build records."""


async def _serve(_epoch: Epoch) -> Any:
    return _Serving()


async def _failing_rebuild(_epoch: Epoch) -> None:
    raise _ProofBuildError("the proposed env does not build")


@pytest.mark.usefixtures("_epoch_state")
async def test_a_discarded_build_leaves_the_current_registries_open() -> None:
    closed: list[str] = []
    await _cache_resources(closed, "current")

    await build_and_discard_epoch(
        {"TAI_LOOP_RESOURCES_TEST_KEY": "proposed"},
        rebuild=no_rebuild,
        build_serving_app=_serve,
        ownership=DeploymentOwnership(),
    )

    assert closed == []
    assert checkpoint_registry().has_live_resources is True
    assert store_registry().has_live_resources is True


@pytest.mark.usefixtures("_epoch_state")
async def test_a_failed_build_leaves_the_current_registries_open() -> None:
    closed: list[str] = []
    await _cache_resources(closed, "current")

    with pytest.raises(_ProofBuildError):
        await build_and_discard_epoch(
            {"TAI_LOOP_RESOURCES_TEST_KEY": "proposed"},
            rebuild=_failing_rebuild,
            build_serving_app=_serve,
            ownership=DeploymentOwnership(),
        )

    assert closed == []
    assert checkpoint_registry().has_live_resources is True


# ---------------------------------------------------------------------------
# The retire closes the retired epoch's registries
# ---------------------------------------------------------------------------


class _Generation:
    """The retired generation's transports: their close is noted, and fails when told to."""

    def __init__(self, steps: list[str], *, fails: bool = False) -> None:
        self.number = 7
        self._steps = steps
        self._fails = fails

    async def close_transports(self) -> None:
        self._steps.append("transports")
        if self._fails:
            raise RuntimeError("transport close failed")


def _pool_drain(steps: list[str], *, fails: bool = False):
    async def _drain(epoch: int, deadline: float) -> None:
        steps.append("pools")
        if fails:
            raise RuntimeError("pool drain failed")

    return _drain


async def test_the_retire_closes_the_retired_epochs_registries_and_keeps_the_current_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    steps: list[str] = []
    await _cache_resources(closed, "retired")
    retired = advance_client_epoch()
    await _cache_resources(closed, "current")
    monkeypatch.setattr(epoch_retire, "drain_epoch", _pool_drain(steps))

    await epoch_retire._close_generation(cast("Epoch", _Generation(steps)), retired, 0.0)

    assert sorted(closed) == ["retired-checkpoint", "retired-store"]
    assert steps == ["transports", "pools"]
    assert checkpoint_registry().has_live_resources is True
    assert store_registry().has_live_resources is True


async def test_the_retired_registries_close_when_the_transport_close_and_the_pool_drain_fail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    closed: list[str] = []
    steps: list[str] = []
    await _cache_resources(closed, "retired")
    retired = advance_client_epoch()
    monkeypatch.setattr(epoch_retire, "drain_epoch", _pool_drain(steps, fails=True))

    with caplog.at_level(logging.ERROR, logger=epoch_retire.__name__):
        await epoch_retire._close_generation(cast("Epoch", _Generation(steps, fails=True)), retired, 0.0)

    assert steps == ["transports", "pools"]
    assert sorted(closed) == ["retired-checkpoint", "retired-store"]
    assert [r.getMessage() for r in caplog.records] == [
        "epoch 7 retire: serving-lifespan close failed",
        f"epoch {retired} retire: client-pool drain failed",
    ]


async def test_a_failing_registry_close_is_logged_and_the_transports_and_pools_still_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    steps: list[str] = []

    async def _failing_close() -> None:
        raise RuntimeError("saver close failed")

    async def _factory() -> tuple[Any, Any]:
        return object(), _failing_close

    await checkpoint_registry()._get_or_init_resource("saver", _factory)
    retired = advance_client_epoch()
    monkeypatch.setattr(epoch_retire, "drain_epoch", _pool_drain(steps))

    with caplog.at_level(logging.ERROR, logger=epoch_retire.__name__):
        await epoch_retire._close_generation(cast("Epoch", _Generation(steps)), retired, 0.0)

    assert steps == ["transports", "pools"]
    failures = [r for r in caplog.records if r.name == epoch_retire.__name__]
    assert [r.getMessage() for r in failures] == [f"epoch {retired} retire: loop-bound registry close failed"]
    assert failures[0].exc_info is not None
    assert isinstance(failures[0].exc_info[1], ExceptionGroup)
