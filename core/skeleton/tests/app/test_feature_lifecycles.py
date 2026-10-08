"""Feature lifecycles run whatever routers ``default_routers`` mounts.

The app registers the conversations lifecycle and the marketplace advisory poll at
construction, not on a router import. The conversations hooks therefore run on a
``default_routers: "none"`` boot with no conversations router, and the advisory poll starts
exactly where the ``marketplace_advisories`` operation is served — read from the committed
route generation of the boot or swap that runs the establisher.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.conversations import delivery_sweep as delivery_sweep_module
from tai42_skeleton.conversations.turn import COMPLETION_TOOL_NAME, DELIVER_TOOL_COMPLETION_NAME
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import advisories

_MARKETPLACE_ROUTER = "tai42_skeleton.routers.marketplace"
_NOT_SERVED = "advisory poll not started — the marketplace_advisories operation is not served"


@pytest.fixture
def poll_starts(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Spy on the poll start with the install-attribution store reading configured."""
    monkeypatch.setattr(advisories, "component_store_configured", lambda component: True)
    started: list[bool] = []
    monkeypatch.setattr(advisories, "start_poll", lambda: started.append(True))
    return started


def _boot(manifest: dict[str, Any]) -> None:
    """Boot a fresh app with the two advisory handlers registered as ``build_app`` registers them."""
    instance = TaiMCP(name="feature-lifecycle-under-test")
    instance.lifecycle.on_post_swap(advisories.start_advisories_poll)
    instance.lifecycle.on_shutdown(advisories.stop_advisories_poll)

    async def run() -> None:
        async with instance.app_context(Manifest.model_validate(manifest)):
            pass

    with tai42_app.bound(None):
        asyncio.run(run())


def test_a_none_boot_without_the_marketplace_router_starts_no_poll(
    poll_starts: list[bool], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.marketplace.advisories"):
        _boot({"default_routers": "none", "routers_modules": ["tai42_skeleton.routers.tools"]})
    assert poll_starts == []
    assert any(_NOT_SERVED in record.getMessage() for record in caplog.records)


def test_a_none_boot_listing_the_marketplace_router_starts_the_poll_once(poll_starts: list[bool]) -> None:
    _boot({"default_routers": "none", "routers_modules": [_MARKETPLACE_ROUTER]})
    assert poll_starts == [True]


def test_an_all_boot_starts_the_poll_once(poll_starts: list[bool]) -> None:
    _boot({"default_routers": "all"})
    assert poll_starts == [True]


def test_the_served_read_is_scoped_to_the_booting_generation(poll_starts: list[bool]) -> None:
    _boot({"default_routers": "all"})
    assert poll_starts == [True]
    poll_starts.clear()
    _boot({"default_routers": "none"})
    assert poll_starts == []


# -- the singleton app, booted and reloaded in place -------------------------------------


@pytest.fixture
def _restore_process_env():
    """A successful build+swap leaves its applied env live; restore it and the settings caches after."""
    from tai42_skeleton.app import epoch as epoch_mod

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()
    reset_all_settings()


def _patch_reload(monkeypatch: pytest.MonkeyPatch, *, manifest: dict[str, Any], env: dict[str, str]) -> None:
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: manifest)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: env)


def _reload_away_from_the_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, *, to_manifest: dict[str, Any]
) -> None:
    """Boot ``"all"`` with the poll alive, reload in place to ``to_manifest``, assert no poll survives."""
    monkeypatch.setattr(advisories, "component_store_configured", lambda component: True)
    monkeypatch.setenv("MARKETPLACE_ADVISORIES_POLL", "true")
    monkeypatch.setenv("MARKETPLACE_ADVISORIES_INTERVAL_S", "3600")
    reset_all_settings()

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "all"})):
            boot_task = advisories._poll_task
            assert boot_task is not None
            assert not boot_task.done()
            _patch_reload(monkeypatch, manifest=to_manifest, env={"ACCESS_CONTROL_ENABLE": "false"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            assert advisories._poll_task is None
            await asyncio.sleep(0)
            assert boot_task.cancelled() or boot_task.done()

    try:
        with caplog.at_level(logging.INFO, logger="tai42_skeleton.marketplace.advisories"):
            asyncio.run(run())
    finally:
        advisories._poll_task = None
        reset_all_settings()


@pytest.mark.usefixtures("_restore_process_env")
def test_a_reload_that_stops_serving_the_operation_cancels_the_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _reload_away_from_the_poll(
        monkeypatch,
        caplog,
        to_manifest={"default_routers": "none", "routers_modules": ["tai42_skeleton.routers.tools"]},
    )
    assert any(_NOT_SERVED in record.getMessage() for record in caplog.records)


@pytest.mark.usefixtures("_restore_process_env")
def test_a_reload_that_finds_the_store_unconfigured_cancels_the_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configured = {"value": True}
    monkeypatch.setattr(advisories, "component_store_configured", lambda component: configured["value"])

    real_reload = app.admin.reload_config

    def _reload_with_the_store_dropped(*args: Any, **kwargs: Any) -> Any:
        configured["value"] = False
        return real_reload(*args, **kwargs)

    monkeypatch.setattr(app.admin, "reload_config", _reload_with_the_store_dropped)
    monkeypatch.setenv("MARKETPLACE_ADVISORIES_POLL", "true")
    monkeypatch.setenv("MARKETPLACE_ADVISORIES_INTERVAL_S", "3600")
    reset_all_settings()

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "all"})):
            boot_task = advisories._poll_task
            assert boot_task is not None
            _patch_reload(monkeypatch, manifest={"default_routers": "all"}, env={"ACCESS_CONTROL_ENABLE": "false"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            assert advisories._poll_task is None
            await asyncio.sleep(0)
            assert boot_task.cancelled() or boot_task.done()

    try:
        with caplog.at_level(logging.INFO, logger="tai42_skeleton.marketplace.advisories"):
            asyncio.run(run())
    finally:
        advisories._poll_task = None
        reset_all_settings()
    assert any("advisory poll skipped" in record.getMessage() for record in caplog.records)


@pytest.mark.usefixtures("_restore_process_env")
def test_a_none_boot_without_the_conversations_router_runs_the_conversations_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A conversations backend is configured; the sweep task sleeps on its long interval.
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    reset_all_settings()
    redriven: list[str] = []

    async def _redrive_accepted() -> None:
        redriven.append("accepted")

    async def _redrive_pending() -> None:
        redriven.append("pending")

    import tai42_skeleton.conversations as conversations_package

    monkeypatch.setattr(conversations_package, "redrive_accepted", _redrive_accepted)
    monkeypatch.setattr(conversations_package, "redrive_pending", _redrive_pending)

    async def run() -> None:
        manifest = {"default_routers": "none", "routers_modules": ["tai42_skeleton.routers.tools"]}
        async with app.app_context(Manifest.model_validate(manifest)):
            for name in (COMPLETION_TOOL_NAME, DELIVER_TOOL_COMPLETION_NAME):
                assert (await app.tools.get_tool(name)).name == name
            sweep = delivery_sweep_module._sweep_task
            assert sweep is not None
            assert not sweep.done()

    try:
        asyncio.run(run())
    finally:
        delivery_sweep_module._sweep_task = None
        reset_all_settings()
    assert redriven == ["accepted", "pending"]
