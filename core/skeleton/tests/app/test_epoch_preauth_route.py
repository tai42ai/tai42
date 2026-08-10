"""RS-B6: a surviving epoch's pre-auth login route returns non-500 after a FAILED build.

A profile-apply reload whose build fails must leave the live epoch — including the
provider instances its ``probe_identity_provider`` recorded — completely untouched, so the
accounts-provider login routes keep resolving the SAME provider through
``tai42_app.accounts.active_provider`` (the current epoch's ``ServingCore``), never a torn
holder that would 500. This drives ``reload_config`` (which collapses onto
``build_and_swap_epoch``) through the reload gate exactly as production does, with a
TEST-LOCAL fake provider + a TEST-LOCAL pre-auth route (the real accounts-oidc plugin is
deliberately NOT a skeleton dev-dependency — the fake through the real epoch machinery
proves the mechanism end to end).
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

# Referenced by string (not imported at collection time): the fixture registers a
# ``custom_route`` at import, which requires the app already bound — the reload machinery
# imports it during ``start()``, exactly as it does the headline router.
_ROUTER = "tests.app._fixtures.preauth_probe_router"
PROVIDER_NAME = "fake_preauth"
PREAUTH_PATH = "/api/preauth-probe"


@pytest.fixture(autouse=True)
def _restore_process_env_and_routes():
    """A successful build+swap leaves its applied env live in ``os.environ``. Snapshot and
    restore it, drop any route the reload added to the process-global route registry, and
    drop the fixture's identity-provider registration so neither leaks into another suite."""
    from tai42_contract.access_control import registry as identity_registry

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
    identity_registry._REGISTRY.pop(PROVIDER_NAME, None)


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


def test_preauth_route_stays_non_500_across_a_failed_build_and_a_success(monkeypatch):
    """The pre-auth route resolves the live epoch's provider (non-500) at boot, after a
    FAILED build (the live epoch is untouched), AND after a subsequent SUCCESSFUL cycle
    (the new epoch recorded + resolves its own provider)."""
    good = {"default_routers": "none", "routers_modules": [_ROUTER]}
    broken = {"default_routers": "none", "storage_module": "totally_bogus_pkg_xyz"}
    # The fake provider is the configured chain, so the real per-epoch probe records it into
    # each generation's ServingCore. Access control's request gate is left OFF so the
    # dispatch exercises the provider-resolution path only, not the verifier's own store —
    # the probe (a registered lifecycle handler) records the provider regardless of the gate.
    auth_env = {"ACCESS_CONTROL_ENABLE": "false", "ACCESS_CONTROL_AUTH_PROVIDERS": f'["{PROVIDER_NAME}"]'}

    async def run() -> None:
        monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
        monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", f'["{PROVIDER_NAME}"]')
        reset_all_settings()
        async with app.app_context(Manifest.model_validate(good)):
            # A pure app_context boot attaches no serving app (the worker lifespan does),
            # so a successful reload establishes the epoch's dispatch handle first — the
            # same warm-up the side-effect-free harness uses.
            _patch_reload(monkeypatch, manifest=good, env=auth_env)
            await reload_gate.run(app.admin.reload_config)

            # The live epoch's probe recorded the fake provider; the route resolves it.
            status, body = await _dispatch_get(current_epoch().serving_app, PREAUTH_PATH)
            assert status == 200, (status, body)
            assert json.loads(body)["provider"] == "_FakePreAuthProvider"

            live = current_epoch()

            # A REAL failing build: the live epoch (and its recorded provider) is untouched.
            _patch_reload(monkeypatch, manifest=broken, env=auth_env)
            with pytest.raises(Exception, match="totally_bogus_pkg_xyz"):
                await reload_gate.run(app.admin.reload_config)

            # The surviving epoch is the same one, and its pre-auth route still resolves the
            # provider — NON-500.
            assert current_epoch() is live
            status, body = await _dispatch_get(current_epoch().serving_app, PREAUTH_PATH)
            assert status != 500, (status, body)
            assert status == 200
            assert json.loads(body)["provider"] == "_FakePreAuthProvider"

            # A subsequent SUCCESSFUL cycle: the new epoch records + resolves its own
            # provider through the fresh serving surface — still NON-500.
            _patch_reload(monkeypatch, manifest=good, env=auth_env)
            await reload_gate.run(app.admin.reload_config)
            assert current_epoch() is not live
            status, body = await _dispatch_get(current_epoch().serving_app, PREAUTH_PATH)
            assert status != 500, (status, body)
            assert status == 200
            assert json.loads(body)["provider"] == "_FakePreAuthProvider"

    try:
        asyncio.run(run())
    finally:
        reset_all_settings()


def test_request_path_resolves_live_provider_during_an_in_flight_build(monkeypatch):
    """A request that interleaves with an in-flight build's serving-app enter must resolve
    the LIVE epoch's provider, never the generation being built.

    The build path instantiates the fresh generation's provider and records it into
    ``app._building`` (``probe_identity_provider``); a request-path reader (the verifier's
    factory) resolves it through ``tai42_app.accounts.active_provider``. That accessor MUST
    return the live instance so a build that then FAILS can never leave a surviving epoch's
    memoized verifier bound onto the discarded generation's provider (the zero-mutation
    invariant). Drives the ``build_and_swap_epoch`` primitive directly with a
    ``build_serving_app`` seam that captures what a request-path read sees at the exact
    ``_building``-is-set window, then fails the build to take the discard branch."""
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app import epoch as epoch_mod

    good = {"default_routers": "none", "routers_modules": [_ROUTER]}
    auth_env = {"ACCESS_CONTROL_ENABLE": "false", "ACCESS_CONTROL_AUTH_PROVIDERS": f'["{PROVIDER_NAME}"]'}

    async def run() -> None:
        monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
        monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", f'["{PROVIDER_NAME}"]')
        reset_all_settings()
        async with app.app_context(Manifest.model_validate(good)):
            _patch_reload(monkeypatch, manifest=good, env=auth_env)
            await reload_gate.run(app.admin.reload_config)

            live = current_epoch()
            live_provider = tai42_app.accounts.active_provider(PROVIDER_NAME)
            assert live_provider is not None

            seen: dict[str, object] = {}

            async def _capture_then_fail(_new_epoch):
                # Mid-build: the default rebuild has already instantiated + recorded the
                # NEW generation's provider into ``app._building``. A request-path read must
                # STILL resolve the live epoch's instance, not the one being built. Clear
                # ``_building`` in a finally exactly as the real ``_default_build_serving_app``
                # does, so a failure leaves no half-built core on the slot.
                try:
                    building = app._building
                    assert building is not None
                    seen["built"] = building.active_auth_providers.get(PROVIDER_NAME)
                    seen["request_path"] = tai42_app.accounts.active_provider(PROVIDER_NAME)
                    raise RuntimeError("totally_bogus_pkg_xyz: forced failure inside the build window")
                finally:
                    app._building = None

            _patch_reload(monkeypatch, manifest=good, env=auth_env)
            with pytest.raises(Exception, match="totally_bogus_pkg_xyz"):
                await epoch_mod.build_and_swap_epoch(dict(os.environ), build_serving_app=_capture_then_fail)

            # The build DID instantiate a distinct new-generation provider (available to leak)...
            assert seen["built"] is not None
            assert seen["built"] is not live_provider
            # ...but the request-path read resolved the LIVE instance, never the built one.
            assert seen["request_path"] is live_provider

            # The failed build left the live epoch and its provider untouched.
            assert current_epoch() is live
            assert tai42_app.accounts.active_provider(PROVIDER_NAME) is live_provider

    try:
        asyncio.run(run())
    finally:
        reset_all_settings()
