"""The fresh-FastMCP-per-epoch headline, driven through the REAL production seams.

A profile-apply reload builds a NEW ``ServingCore`` (a fresh FastMCP) off to the side
and swaps it in: a router added during the reload ACTUALLY SERVES after the swap,
a failed build keeps the old surface serving with the env restored and zero
live-state mutation, and every startup handler re-runs per epoch so an eagerly
built provider is authoritative for its pre-auth route.

These drive ``reload_config`` (which collapses onto ``build_and_swap_epoch``) through
the reload gate exactly as production does — the REAL rebuild + the REAL fresh
``http_app``, not injected sentinels.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.epoch import current_epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.manifest import Manifest

_HEADLINE_ROUTER = "tests.app._fixtures.headline_router"
_HEADLINE_PATH = "/api/headline-probe"


@pytest.fixture(autouse=True)
def _restore_process_env():
    """A successful build+swap leaves its applied env live in ``os.environ`` (the
    caller persists it in production). Snapshot and restore it around each test so a
    reload's proposed env (e.g. ``ACCESS_CONTROL_ENABLE=false``) never leaks into the
    next test, and reset the epoch env-tracking + settings caches with it."""
    from tai42_skeleton.app import epoch as epoch_mod
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    routes_before = dict(route_registry._routes)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()
    reset_all_settings()
    # The process-global route registry dedups and is never cleared, so drop the
    # headline fixture route the reload added — else it leaks into the CLI-parity and
    # route-coverage gates.
    for key in list(route_registry._routes):
        if key not in routes_before:
            del route_registry._routes[key]


async def _dispatch_get(serving_app, path: str) -> tuple[int, bytes]:
    """Dispatch a bare GET into a live ASGI serving app and return (status, body).

    The serving app's FastMCP lifespan was already entered by the swap's supervisor,
    so a plain custom-route GET is served without re-entering it."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await serving_app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def _patch_reload(monkeypatch, *, manifest: dict, env: dict[str, str]) -> None:
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: manifest)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: env)


def test_reload_added_router_actually_serves_after_the_epoch_cycle(monkeypatch):
    """THE headline: a router the reload adds is served by the swapped-in epoch's
    fresh ``http_app`` — the old epoch's snapshot never included it."""
    # Access control OFF for the served epoch so the custom route answers directly
    # rather than being resolved by the resource guard; ``default_routers=none`` keeps
    # the surface minimal (no SPA catch-all to shadow an absent route).
    reload_env = {"ACCESS_CONTROL_ENABLE": "false"}
    boot_manifest = {"default_routers": "none"}
    with_router = {"default_routers": "none", "routers_modules": [_HEADLINE_ROUTER]}

    async def run() -> None:
        async with app.app_context(Manifest.model_validate(boot_manifest)):
            _patch_reload(monkeypatch, manifest=boot_manifest, env=reload_env)
            # First reload lands epoch 1 (AC off, no headline router yet): the route is
            # absent from this generation's fresh http_app.
            await reload_gate.run(app.admin.reload_config, reimports=True)
            before = current_epoch().serving_app
            assert before is not None
            status, _ = await _dispatch_get(before, _HEADLINE_PATH)
            assert status == 404

            # Now reload with the router module added: the next epoch's FRESH FastMCP
            # snapshots the new route table, so the swapped-in http_app serves it.
            _patch_reload(monkeypatch, manifest=with_router, env=reload_env)
            await reload_gate.run(app.admin.reload_config, reimports=True)
            after = current_epoch().serving_app
            assert after is not None
            assert after is not before
            status, body = await _dispatch_get(after, _HEADLINE_PATH)
            assert status == 200, (status, body)
            assert json.loads(body) == {"served": True}

    try:
        asyncio.run(run())
    finally:
        reset_all_settings()


def test_failed_build_keeps_old_surface_serving_and_restores_env(monkeypatch):
    """A reload whose build fails (a broken scalar-slot plugin) is discarded with the
    old epoch still serving, the process env restored exactly, and the failure raised
    loudly — driven through the REAL rebuild + a REAL fresh FastMCP."""
    good = {"default_routers": "none", "routers_modules": [_HEADLINE_ROUTER]}
    broken = {"default_routers": "none", "storage_module": "totally_bogus_pkg_xyz"}
    reload_env = {"ACCESS_CONTROL_ENABLE": "false"}

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            _patch_reload(monkeypatch, manifest=good, env=reload_env)
            await reload_gate.run(app.admin.reload_config, reimports=True)
            live = current_epoch()
            live_core = live.core
            # The good epoch serves the headline route.
            status, _ = await _dispatch_get(live.serving_app, _HEADLINE_PATH)
            assert status == 200

            os.environ["TAI_EPOCH_FAIL_MARKER"] = "sentinel"
            env_before = dict(os.environ)

            # A build against a broken scalar slot: the build aborts, the half-built
            # epoch is discarded, and the failure is raised loudly.
            _patch_reload(monkeypatch, manifest=broken, env={"ACCESS_CONTROL_ENABLE": "false", "TAI_EPOCH_NEW": "x"})
            with pytest.raises(Exception, match="totally_bogus_pkg_xyz"):
                await reload_gate.run(app.admin.reload_config, reimports=True)

            # Zero live-state mutation: the same epoch + core still serve.
            assert current_epoch() is live
            assert current_epoch().core is live_core
            status, body = await _dispatch_get(current_epoch().serving_app, _HEADLINE_PATH)
            assert status == 200, (status, body)
            assert json.loads(body) == {"served": True}
            # os.environ restored EXACTLY (the proposed key never lingered).
            assert dict(os.environ) == env_before
            assert "TAI_EPOCH_NEW" not in os.environ

    try:
        asyncio.run(run())
    finally:
        os.environ.pop("TAI_EPOCH_FAIL_MARKER", None)
        os.environ.pop("TAI_EPOCH_NEW", None)
        reset_all_settings()


def test_startup_handlers_run_per_epoch(monkeypatch):
    """Every startup handler re-runs on each epoch build (the mechanism that makes an
    eagerly-instantiated provider authoritative for its pre-auth route), not
    only at cold boot."""
    fired: list[int] = []

    def _startup_marker() -> None:
        fired.append(1)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            app.lifecycle.on_startup(_startup_marker)
            fired.clear()
            _patch_reload(monkeypatch, manifest={"default_routers": "none"}, env={"ACCESS_CONTROL_ENABLE": "false"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            # The startup handler ran during the epoch rebuild, not just at boot.
            assert fired == [1]

    try:
        asyncio.run(run())
    finally:
        app._startup_handlers.pop(f"{__name__}._startup_marker", None)
        reset_all_settings()
