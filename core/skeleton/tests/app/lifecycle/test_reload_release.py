"""The soft config reload releases every loop-bound kit registry before its build, on the serving loop."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry

from tai42_skeleton.app import epoch as epoch_mod

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

    async def _build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Any:
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

    async def _build(env: dict[str, str], *, drain_tolerate_driver: bool) -> Any:
        order.append("build")

    monkeypatch.setattr("tai42_skeleton.app.lifecycle.release_loop_bound_resources", _release)
    monkeypatch.setattr(epoch_mod, "build_and_swap_epoch", _build)
    mixin = _Mixin()
    mixin._config_manager = _EnvConfig()  # pyright: ignore[reportAttributeAccessIssue]
    mixin._serving_loop = asyncio.get_running_loop()

    assert await asyncio.to_thread(mixin._reload_config) == {"status": "ok", "env_keys": 1}
    assert order == ["release", "build"]
