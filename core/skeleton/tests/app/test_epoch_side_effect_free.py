"""The side-effect-free-build proof: a FAILED epoch build leaves ZERO global mutation.

A profile-apply reload whose build fails must discard the half-built epoch with the
live epoch untouched. These drive ``reload_config`` (which collapses onto
``build_and_swap_epoch``) through the reload gate exactly as production does, and pin
the specific globals the side-effect-free sweep protects:

* the four generation registries (connector, identity, accounts, operation) are
  BIT-IDENTICAL to their pre-build committed contents — a staged build's registrations
  never reach the committed maps the request path reads;
* the process spine fields ``_manifest`` / ``_failed_mcps`` / ``_mcp_bound_tools`` are
  the same live-epoch objects, unchanged;
* the old serving surface keeps answering, and the env is restored exactly.

The failing build uses a broken scalar-slot plugin, so the abort fires deep in the REAL
rebuild after ``start()`` has already staged the fresh generation's registries — the
exact window the staged-commit primitive protects.
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
    """A successful build+swap leaves its applied env live in ``os.environ``. Snapshot
    and restore it around each test, and drop any route the reload added to the
    process-global route registry so it never leaks into another suite's gates."""
    from tai42_skeleton.app import epoch as epoch_mod
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    routes_before = dict(route_registry._routes)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()
    reset_all_settings()
    for key in list(route_registry._routes):
        if key not in routes_before:
            del route_registry._routes[key]


async def _dispatch_get(serving_app, path: str) -> tuple[int, bytes]:
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


def _committed_registry_snapshots() -> dict[str, dict]:
    """A deep-enough snapshot of the four generation registries' COMMITTED contents."""
    from tai42_contract.access_control import registry as identity_registry
    from tai42_contract.accounts import registry as accounts_registry

    from tai42_skeleton.connectors.providers import registry as connector_registry
    from tai42_skeleton.operations.registry import operation_registry

    return {
        "connector": dict(connector_registry._REGISTRY),
        "identity": dict(identity_registry._REGISTRY),
        "accounts": dict(accounts_registry._REGISTRY),
        "operation": dict(operation_registry._operations),
    }


def test_failed_build_leaves_the_four_registries_bit_identical(monkeypatch):
    """The connector/identity/accounts/operation registries the request path reads are
    UNCHANGED after a failed build — the staged generation's registrations never touched
    the committed maps."""
    good = {"default_routers": "none", "routers_modules": [_HEADLINE_ROUTER]}
    broken = {"default_routers": "none", "storage_module": "totally_bogus_pkg_xyz"}
    reload_env = {"ACCESS_CONTROL_ENABLE": "false"}

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            _patch_reload(monkeypatch, manifest=good, env=reload_env)
            await reload_gate.run(app.admin.reload_config, reimports=True)

            live = current_epoch()
            live_core = live.core
            before = _committed_registry_snapshots()
            manifest_before = app._manifest
            failed_before = dict(app._failed_mcps)
            bound_before = {t: set(v) for t, v in app._mcp_bound_tools.items()}

            _patch_reload(monkeypatch, manifest=broken, env={"ACCESS_CONTROL_ENABLE": "false", "TAI_EPOCH_NEW": "x"})
            with pytest.raises(Exception, match="totally_bogus_pkg_xyz"):
                await reload_gate.run(app.admin.reload_config, reimports=True)

            # The four committed registries are bit-identical to their pre-build contents.
            assert _committed_registry_snapshots() == before

            # The live epoch, its core, and the spine fields are the SAME, unchanged.
            assert current_epoch() is live
            assert current_epoch().core is live_core
            assert app._manifest is manifest_before
            assert dict(app._failed_mcps) == failed_before
            assert {t: set(v) for t, v in app._mcp_bound_tools.items()} == bound_before

            # The old surface keeps serving, and the proposed env never lingered.
            status, body = await _dispatch_get(current_epoch().serving_app, _HEADLINE_PATH)
            assert status == 200, (status, body)
            assert json.loads(body) == {"served": True}
            assert "TAI_EPOCH_NEW" not in os.environ

    try:
        asyncio.run(run())
    finally:
        os.environ.pop("TAI_EPOCH_NEW", None)
        reset_all_settings()


def test_reload_with_open_loop_bound_checkpoint_does_not_raise(monkeypatch):
    """RB-M1: a reload with a live loop-bound checkpoint resource open succeeds — the
    reload closes the langgraph checkpoint/store registries BEFORE the build's settings
    reset drops their per-loop registries, so the reset never finds a registry still
    holding live resources on the running loop (close-before-reset ordering guarded)."""
    from tai42_kit.llm.checkpoint import checkpoint_registry as reg_mod

    async def _fake_create(provider, conn_string):
        async def _closer() -> None:
            pass

        return (object(), _closer)

    monkeypatch.setattr(reg_mod, "create_checkpoint_resource", _fake_create)
    monkeypatch.setattr(reg_mod, "get_saver_from_resource", lambda provider, resource: resource)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({"default_routers": "none"})):
            # Open a live loop-bound checkpoint resource on the serving loop the reload's
            # close-and-swap runs on.
            reg = reg_mod.checkpoint_registry()
            await reg.get_checkpointer("memory", "c1")
            assert reg.has_live_resources is True

            _patch_reload(monkeypatch, manifest={"default_routers": "none"}, env={"ACCESS_CONTROL_ENABLE": "false"})
            # The reload MUST NOT raise "still holds live resources": ``reload_config``
            # closes the LLM registries before the build resets settings.
            await reload_gate.run(app.admin.reload_config, reimports=True)

            # The reload rebuilt the registry (closed + dropped), so the live one is fresh.
            assert reg_mod.checkpoint_registry() is not reg

    try:
        asyncio.run(run())
    finally:
        reset_all_settings()
