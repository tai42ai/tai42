"""The epoch build restores ``os.environ`` exactly on EVERY failure — including one raised while
the proposed env is applied and the settings reset, or while the staged generations open."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from starlette.types import ASGIApp
from tai42_contract.app import tai42_app
from tai42_kit.access_control import registry as identity_registry
from tai42_kit.accounts import registry as accounts_registry
from tai42_kit.clients.base import current_client_epoch
from tai42_kit.settings import cache_registry
from tai42_kit.settings.cache_registry import register_settings_reset
from tai42_kit.utils import worker_secret_capability as gate_state

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import registry_staging
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch, current_epoch
from tai42_skeleton.app.instance import build_app
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.connectors.providers import registry as connector_registry
from tai42_skeleton.monitoring import registry as monitoring_registry
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.plugins import registry as studio_registry

# Bind the process app handle so the build primitive's imports resolve as in production.
tai42_app.bind(build_app())

_MARKER = "TAI_EPOCH_ENV_RESTORE_MARKER"


class _HookRefusedError(RuntimeError):
    pass


class _StagingRefusedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _isolated_epoch() -> Iterator[None]:
    """A boot epoch to keep serving, and the spine globals restored after."""
    saved = {name: getattr(epoch_mod, name) for name in ("_current", "_serving_slot")}
    loaded_before = set(epoch_mod._loaded_env_keys)
    epoch_mod._current = Epoch(number=current_client_epoch(), serving_app=None)
    epoch_mod._serving_slot = {}
    epoch_mod._loaded_env_keys = {"TAI_EPOCH_ENV_RESTORE_LOADED"}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(epoch_mod, name, value)
        epoch_mod._loaded_env_keys = loaded_before


def _no_open_staged_generation() -> bool:
    return not any(
        (
            connector_registry._GENERATION.staging,
            identity_registry._PROVIDERS._generation.staging,
            accounts_registry._PROVIDERS._generation.staging,
            operation_registry._generation.staging,
            route_registry._shapes.staging,
            monitoring_registry._BACKEND._staging,
            studio_registry._REGISTRY._staging,
            gate_state._GATE_STATE._staging,
        )
    )


async def _never_built(_epoch: Epoch) -> ASGIApp:  # pragma: no cover - the build fails before it
    raise AssertionError("the serving app is never built on a failed build")


async def _assert_restored_after(proposed: dict[str, str], error: type[Exception]) -> None:
    live = current_epoch()
    env_before = dict(os.environ)
    loaded_before = set(epoch_mod._loaded_env_keys)

    with pytest.raises(error):
        await build_and_swap_epoch(proposed, rebuild=lambda: None, build_serving_app=_never_built)

    assert dict(os.environ) == env_before
    assert _MARKER not in os.environ
    assert epoch_mod._loaded_env_keys == loaded_before
    assert current_epoch() is live
    assert _no_open_staged_generation()


async def test_a_refusing_settings_reset_hook_restores_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse_the_marker() -> None:
        if _MARKER in os.environ:
            raise _HookRefusedError("a settings reset hook refused the proposed env")

    register_settings_reset(_refuse_the_marker)
    try:
        await _assert_restored_after({_MARKER: "proposed"}, _HookRefusedError)
    finally:
        cache_registry._RESET_HOOKS.pop(cache_registry._key(_refuse_the_marker), None)


async def test_a_staged_generation_that_fails_to_open_restores_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    real_begin = registry_staging.begin_staging_all

    def _begin_then_refuse() -> None:
        real_begin()
        raise _StagingRefusedError("a staged generation refused to open")

    monkeypatch.setattr(registry_staging, "begin_staging_all", _begin_then_refuse)
    await _assert_restored_after({_MARKER: "proposed"}, _StagingRefusedError)
