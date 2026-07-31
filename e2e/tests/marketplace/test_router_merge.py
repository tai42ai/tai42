"""C7 — the plugin ROUTER/MIDDLEWARE auto-merge, end to end. Opt-in: collects only
with ``TAI_E2E_MARKETPLACE=1``.

Installing a plugin that provides a ``router`` (and a ``middleware``) proves README
D2 + D4 compose: the loader owns the SPA catch-all's last position, and the merge
inserts the plugin's router BEFORE it. The served ASGI route table is a boot-time
snapshot, so the deliverable is PERSIST + activate-on-restart — NOT a hot route:

* After install (no restart): the persisted manifest CONTAINS the plugin's router
  module immediately before ``tai42_skeleton.routers.plugins`` (and its middleware in
  ``middlewares_modules``), but ``GET /api/e2e-epsilon/ping`` is STILL 404 in the
  running process (a mere reload does not hot-load a new router — asserting non-404
  here would be testing the deferred hot-load feature, which is out of scope).
* After a RESTART on the now-persisted manifest: the route is LIVE and answers the
  epsilon marker — reachable because it precedes the catch-all, not shadowed behind
  it — and the middleware's response header is present.
* Uninstall + restart reverts: the module leaves the manifest (catch-all still last)
  and the route 404s again; the venv guard confirms the distribution is gone.
"""

from __future__ import annotations

import pytest
import yaml
from _market_support import distribution_absent

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
_PING = "/api/e2e-epsilon/ping"


def _persisted_manifest(stack: TaiStack) -> dict:
    """The stack's persisted ``manifest.yml`` on disk — the file the installer's
    config pipeline writes the merged manifest back to and the file a restart
    re-reads."""
    text = (stack.root / "config" / "manifest.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


async def _ping_status(stack: TaiStack) -> int:
    resp = await stack.api().request_raw("GET", _PING)
    return resp.status_code


async def test_router_and_middleware_merge_persist_then_restart(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    router_merge_stack: TaiStack,
) -> None:
    stack = router_merge_stack

    # Publish epsilon into THIS spec's registry only — it is kept out of the shared
    # browse catalog, so the router-merge spec seeds the fixture it installs itself.
    await seed_epsilon_listing(marketplace_service, package_index, fixture_artifacts)

    # Precondition (negative first): the route is dark and the distribution is absent.
    assert await _ping_status(stack) == 404
    assert distribution_absent(EPSILON_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": EPSILON_REF, "version": "0.1.0"})

    # (a) PERSISTED before the SPA catch-all — the primary assertion. No restart yet.
    manifest = _persisted_manifest(stack)
    routers = manifest.get("routers_modules") or []
    middlewares = manifest.get("middlewares_modules") or []
    assert _ROUTER_MODULE in routers, f"router module not persisted into routers_modules: {routers}"
    assert _SPA_CATCH_ALL in routers, f"SPA catch-all missing from routers_modules: {routers}"
    assert routers.index(_ROUTER_MODULE) == routers.index(_SPA_CATCH_ALL) - 1, (
        f"router module is not immediately before the SPA catch-all: {routers}"
    )
    assert _MIDDLEWARE_MODULE in middlewares, f"middleware module not persisted into middlewares_modules: {middlewares}"
    # A mere install reload does NOT make a new router path live in the running
    # process — the route table is a boot-time snapshot.
    assert await _ping_status(stack) == 404

    # (b) After a RESTART the route is LIVE, ordered before the catch-all, and the
    # merged middleware is active.
    stack.restart("serve")
    resp = await stack.api().request_raw("GET", _PING)
    assert resp.status_code != 404, f"route still dark after restart: {resp.status_code}; body {resp.text}"
    body = resp.json()
    assert body["data"]["epsilon"] == "pong", f"epsilon handler did not answer (catch-all shadowed it?): {body}"
    assert resp.headers.get("x-e2e-epsilon") == "1", "the merged middleware's response header is absent after restart"

    # Uninstall removes the module from the persisted manifest (catch-all still last);
    # after a restart the route 404s again and the venv is clean.
    await stack.api().post("/api/marketplace/uninstall", json={"ref": EPSILON_REF})
    after = _persisted_manifest(stack)
    routers_after = after.get("routers_modules") or []
    assert _ROUTER_MODULE not in routers_after, f"router module lingered after uninstall: {routers_after}"
    assert routers_after[-1] == _SPA_CATCH_ALL, f"SPA catch-all is no longer last after uninstall: {routers_after}"

    stack.restart("serve")
    assert await _ping_status(stack) == 404
    assert distribution_absent(EPSILON_PACKAGE)
