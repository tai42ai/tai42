"""C7 — the plugin ROUTER/MIDDLEWARE auto-merge, end to end. Opt-in: collects only
with ``TAI_E2E_MARKETPLACE=1``.

Installing a plugin that provides a ``router`` (and a ``middleware``) proves the README router and
middleware merges compose: the loader owns the SPA catch-all's last position, and the merge
inserts the plugin's router BEFORE it. The serving surface is rebuilt fresh
PER EPOCH — ``epoch._default_build_serving_app`` builds a fresh ``http_app`` off the
reloaded core each swap, so its route table (including a reload-added router) is
snapshotted anew and actually serves (``lifecycle._reload_config``: "a reload-added
router serves after the swap"). So an install reload HOT-LOADS the router — it serves 200
immediately, no restart — and this test asserts that landed contract:

* After install (no restart): the persisted manifest CONTAINS the plugin's router module
  immediately before ``tai42_skeleton.routers.plugins`` (and its middleware in
  ``middlewares_modules``), AND ``GET /api/e2e-epsilon/ping`` answers 200 in the running
  process — reachable because it precedes the catch-all, not shadowed behind it — with the
  merged middleware's response header present. No restart needed.
* A subsequent RESTART re-boots on the now-persisted manifest and STILL serves the route.
* Uninstall HOT-UNLOADS: its reload rebuilds the serving surface WITHOUT the module, so the
  route 404s again with no restart, the module leaves the manifest (catch-all still last),
  and the venv guard confirms the distribution is gone.
"""

from __future__ import annotations

import pytest
import yaml
from _market_support import (
    MarketInstaller,
    distribution_absent,
    skip_unless_registry_supports_declared_routes,
)

from tai42_e2e import wait_for_async
from tai42_e2e.marketplace import (
    EPSILON_PACKAGE,
    EPSILON_REF,
    FixtureArtifacts,
    MarketplaceService,
    seed_epsilon_listing,
)
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

_ROUTER_MODULE = "tai_e2e_market_epsilon.router"
_MIDDLEWARE_MODULE = "tai_e2e_market_epsilon.mw"
_SPA_CATCH_ALL = "tai42_skeleton.routers.plugins"
# The router item declares three routes in tai-plugin.yml relative to its ``e2e-epsilon``
# base — an authed ``/ping``, a public ``/open``, and an authed ``POST /open`` — resolving
# under the default base to these absolute paths (default bases reproduce the pre-declaration
# paths).
_PING = "/api/e2e-epsilon/ping"
_OPEN = "/api/e2e-epsilon/open"


def _persisted_manifest(stack: TaiStack) -> dict:
    """The stack's persisted ``manifest.yml`` on disk — the file the installer's
    config pipeline writes the merged manifest back to and the file a restart
    re-reads."""
    text = (stack.root / "config" / "manifest.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


async def _route_status(stack: TaiStack, path: str) -> int:
    resp = await stack.api().request_raw("GET", path)
    return resp.status_code


async def _wait_route(stack: TaiStack, path: str, expected: int) -> None:
    """Poll ``GET path`` until it reaches ``expected`` — the hot load/unload lands the
    instant the swap completes, but a bounded wait absorbs any reload-tail scheduling;
    a genuine miss still fails at the deadline."""

    async def reached() -> bool:
        return await _route_status(stack, path) == expected

    await wait_for_async(reached, deadline=15.0, message=f"{path} never reached status {expected}")


async def test_router_and_middleware_merge_hot_loads_then_survives_restart(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    router_merge_stack: TaiStack,
    market_installer: MarketInstaller,
) -> None:
    # Epsilon declares routes; skip when the pinned registry can't accept them (the
    # marketplace_service fixture has already built the registry venv the gate reads).
    skip_unless_registry_supports_declared_routes()
    stack = router_merge_stack

    # Publish epsilon into THIS spec's registry only — it is kept out of the shared
    # browse catalog, so the router-merge spec seeds the fixture it installs itself.
    await seed_epsilon_listing(marketplace_service, package_index, fixture_artifacts)

    # Precondition (negative first): both declared routes are dark and the
    # distribution is absent.
    assert await _route_status(stack, _PING) == 404
    assert await _route_status(stack, _OPEN) == 404
    assert distribution_absent(EPSILON_PACKAGE)

    # Install through the abort-safe ledger so a mid-body failure still uninstalls.
    # Epsilon declares a PUBLIC route (GET /api/e2e-epsilon/open), so acceptance is
    # required or the routes-capable registry returns 400 PUBLIC_ROUTES_NOT_ACCEPTED.
    await market_installer.install(stack, EPSILON_REF, EPSILON_PACKAGE, version="0.1.0", accept_public_routes=True)

    # (a) PERSISTED before the SPA catch-all.
    manifest = _persisted_manifest(stack)
    routers = manifest.get("routers_modules") or []
    middlewares = manifest.get("middlewares_modules") or []
    assert _ROUTER_MODULE in routers, f"router module not persisted into routers_modules: {routers}"
    assert _SPA_CATCH_ALL in routers, f"SPA catch-all missing from routers_modules: {routers}"
    assert routers.index(_ROUTER_MODULE) == routers.index(_SPA_CATCH_ALL) - 1, (
        f"router module is not immediately before the SPA catch-all: {routers}"
    )
    assert _MIDDLEWARE_MODULE in middlewares, f"middleware module not persisted into middlewares_modules: {middlewares}"

    # (b) HOT-LOADED (no restart): the install reload built a fresh serving surface, so the
    # merged router serves 200 immediately — ahead of the catch-all — and the merged
    # middleware's header is present.
    await _wait_route(stack, _PING, 200)
    resp = await stack.api().request_raw("GET", _PING)
    assert resp.status_code == 200, f"router did not hot-load after install: {resp.status_code}; body {resp.text}"
    body = resp.json()
    assert body["data"]["epsilon"] == "pong", f"epsilon handler did not answer (catch-all shadowed it?): {body}"
    assert resp.headers.get("x-e2e-epsilon") == "1", "the merged middleware's response header is absent after install"

    # The declared PUBLIC sibling route mounts alongside it (both declared rows land).
    await _wait_route(stack, _OPEN, 200)
    open_resp = await stack.api().request_raw("GET", _OPEN)
    assert open_resp.status_code == 200, f"declared public sibling did not hot-load: {open_resp.status_code}"
    assert open_resp.json()["data"]["epsilon"] == "open", "the declared public sibling handler did not answer"

    # (c) A RESTART re-boots on the persisted manifest and STILL serves the route + header.
    stack.restart("serve")
    resp = await stack.api().request_raw("GET", _PING)
    assert resp.status_code == 200, (
        f"route dark after restart on the persisted manifest: {resp.status_code}; {resp.text}"
    )
    assert resp.json()["data"]["epsilon"] == "pong", "epsilon handler did not answer after restart"
    assert resp.headers.get("x-e2e-epsilon") == "1", "the merged middleware's response header is absent after restart"

    # Uninstall HOT-UNLOADS: the reload rebuilds the surface without the module, so the
    # route 404s again with no restart, the module leaves the manifest (catch-all still
    # last), and the distribution is gone from the venv.
    await stack.api().post("/api/marketplace/uninstall", json={"ref": EPSILON_REF})
    await _wait_route(stack, _PING, 404)
    await _wait_route(stack, _OPEN, 404)
    after = _persisted_manifest(stack)
    routers_after = after.get("routers_modules") or []
    assert _ROUTER_MODULE not in routers_after, f"router module lingered after uninstall: {routers_after}"
    assert routers_after[-1] == _SPA_CATCH_ALL, f"SPA catch-all is no longer last after uninstall: {routers_after}"
    assert distribution_absent(EPSILON_PACKAGE)
