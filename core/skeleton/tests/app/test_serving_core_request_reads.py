"""Request-path facet reads resolve the LIVE generation while a rebuild builds the next one.

Which generation a read belongs to is a property of the work that performs it: the build's own
steps read the generation under construction, and every other piece of work — a request on any
door — reads the generation still serving. These drive the real rebuild and serving-app seams of
``build_and_swap_epoch``, with the manifest read from the config manager replaced by an in-memory
one.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from typing import Any

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import epoch_defaults, instance
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch, current_epoch
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.manifest import Manifest

_MANIFEST: dict[str, Any] = {
    "default_routers": "none",
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather"]}],
    "channel_modules": ["tests.app._fixtures.channel_mod"],
}


@pytest.fixture(autouse=True)
def _restore_process_env():
    """A successful build+swap leaves its applied env and its routes live; restore both around each test."""
    from tai42_skeleton.app import applied_env
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    routes_before = dict(route_registry._routes)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = set()
    reset_all_settings()
    for key in list(route_registry._routes):
        if key not in routes_before:
            del route_registry._routes[key]


def _reload_from(monkeypatch: pytest.MonkeyPatch, manifest: dict[str, Any]) -> None:
    monkeypatch.setattr(instance.app.config.config_manager, "read_manifest", lambda: manifest)


async def _tool_names() -> list[str]:
    return sorted(await instance.app.tools.get_tools())


def test_request_reads_during_a_rebuild_answer_the_live_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = instance.app
    seen: dict[str, Any] = {}

    async def run() -> None:
        async with app.app_context(Manifest.model_validate(_MANIFEST)):
            live_tools = await _tool_names()
            live_channels = app.channels.names()
            assert live_tools
            assert live_channels
            _reload_from(monkeypatch, _MANIFEST)
            real_reload_registries = app._reload_registries

            async def _request() -> None:
                seen["tools"] = await _tool_names()
                seen["channels"] = app.channels.names()

            async def _reload_registries(manifest: Manifest) -> dict[str, Any]:
                # The build is under way and its own start() has not filled the new generation yet:
                # a request served now (an unmarked door) must answer the generation still serving.
                await asyncio.create_task(_request(), context=contextvars.Context())
                seen["build_core"] = app._serving_core
                return await real_reload_registries(manifest)

            monkeypatch.setattr(app, "_reload_registries", _reload_registries)
            await build_and_swap_epoch({}, ownership=DeploymentOwnership())

            assert seen["tools"] == live_tools
            assert seen["channels"] == live_channels
            # The build step itself read the core of the generation it built, now the live one.
            assert seen["build_core"] is current_epoch().core

    asyncio.run(run())


def test_a_build_step_read_before_its_core_exists_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    app = instance.app
    real_build_serving_core = app._build_serving_core

    def _build_serving_core() -> Any:
        # A build-step read of the serving core before the rebuild installed the generation's core.
        app._tool_registry  # noqa: B018
        return real_build_serving_core()

    async def run() -> None:
        async with app.app_context(Manifest.model_validate(_MANIFEST)):
            live = current_epoch()
            _reload_from(monkeypatch, _MANIFEST)
            with monkeypatch.context() as patched:
                patched.setattr(app, "_build_serving_core", _build_serving_core)
                with pytest.raises(RuntimeError, match="this build work belongs to, has no serving core"):
                    await build_and_swap_epoch({}, ownership=DeploymentOwnership())
            assert current_epoch() is live

    asyncio.run(run())


def test_a_task_born_in_the_build_reads_its_generation_once_swapped_in(monkeypatch: pytest.MonkeyPatch) -> None:
    app = instance.app
    release = asyncio.Event()
    born: dict[str, Any] = {}

    async def _long_lived() -> Any:
        await release.wait()
        return app._serving_core

    async def _build_serving_app(epoch: Epoch) -> Any:
        # A long-lived task the build step creates inherits the build's context.
        born["task"] = asyncio.create_task(_long_lived())
        return await epoch_defaults.default_build_serving_app(epoch)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate(_MANIFEST)):
            _reload_from(monkeypatch, _MANIFEST)
            swapped = await build_and_swap_epoch(
                {}, ownership=DeploymentOwnership(), build_serving_app=_build_serving_app
            )
            release.set()
            read = await born["task"]
            assert read is current_epoch().core
            assert current_epoch() is swapped

    asyncio.run(run())
