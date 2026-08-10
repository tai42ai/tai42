"""The route-coverage guard — the dark-pages guard.

The bug class this pins: a manifest that omits a router leaves the endpoint behind a
Studio nav page unmounted, so the page goes dark (its API answers 404). A bare manifest
(``default_routers="all"`` with no ``routers_modules``) serves the whole Studio surface;
this suite boots exactly that and asserts every Studio feature route answers non-404.

Two layers, the primary INDEPENDENT of ``DEFAULT_API_ROUTERS`` (de-circularizing):

* PRIMARY — :data:`STUDIO_ROUTE_ANCHORS` hardcodes one literal endpoint per default
  router. Asserting non-404 does NOT read the skeleton's default tuple, so a wrong default
  set shows up as a literal path going dark rather than regenerating from the same
  constant the loader uses.
* SECONDARY — :func:`test_anchor_set_matches_default_routers` cross-checks the anchor set
  against ``DEFAULT_API_ROUTERS``: an added/removed default router with no matching anchor
  is a loud STOP, so the two lists cannot silently drift.

Non-404 is the assertion — 200/401/403/422 (and a 5xx from an absent backing service) all
mean MOUNTED; only 404 means dark.
"""

from __future__ import annotations

import httpx
import pytest
from tai42_skeleton.app.route_defaults import DEFAULT_API_ROUTERS

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# This module drives no backend worker — its stack is route-mounting only.
pytestmark = pytest.mark.backendless

# INDEPENDENT ground truth: one literal, non-parametric request per default router
# guaranteed non-404 by ROUTING (the route exists) regardless of stored data — a
# collection GET, or a body-rejecting POST whose HTTP-edge validation (422/400) runs
# before any lookup. Keyed by router module so the secondary check can compare keys to
# ``DEFAULT_API_ROUTERS``; the paths are hand-pinned literals, never generated from that
# tuple. Each value is ``(method, path, json)``.
STUDIO_ROUTE_ANCHORS: dict[str, tuple[str, str, dict | None]] = {
    "tai42_skeleton.routers.agents": ("GET", "/api/agents", None),
    "tai42_skeleton.routers.api_keys": ("GET", "/api/auth/scopes", None),
    "tai42_skeleton.routers.backend": ("GET", "/api/backend", None),
    "tai42_skeleton.routers.backup": ("GET", "/api/backup/sections", None),
    "tai42_skeleton.routers.channels": ("GET", "/api/channels", None),
    # Gate off on the default stack, and the default checkpoint provider has no swept
    # store, so the fenced sweep is a mounted no-op (200) with nothing purged.
    "tai42_skeleton.routers.checkpoints": ("POST", "/api/checkpoints/sweep", {}),
    "tai42_skeleton.routers.config": ("GET", "/api/config/mode", None),
    "tai42_skeleton.routers.connectors": ("GET", "/api/connectors/connections", None),
    "tai42_skeleton.routers.conversations": ("GET", "/api/conversations", None),
    "tai42_skeleton.routers.extensions": ("GET", "/api/extensions", None),
    "tai42_skeleton.routers.health": ("GET", "/health", None),
    "tai42_skeleton.routers.hooks": ("GET", "/api/hooks", None),
    # A missing/empty answer body is rejected at the HTTP edge (400) before the
    # interaction is ever looked up, so this proves the route is mounted without a
    # seeded interaction.
    "tai42_skeleton.routers.interactions": ("POST", "/api/interactions/e2e-probe/answer", {}),
    "tai42_skeleton.routers.login": ("GET", "/api/login/methods", None),
    "tai42_skeleton.routers.manifest": ("GET", "/api/manifest", None),
    "tai42_skeleton.routers.marketplace": ("GET", "/api/marketplace/installed", None),
    # A concrete non-``/api`` GET (like ``/health``). The route is ``authed=True``, but
    # non-404 is the mounted verdict: on this bare stack the request answers (auth off ->
    # 200; a pinned/guarded stack -> 401/403) rather than 404, proving it is mounted.
    "tai42_skeleton.routers.metrics": ("GET", "/metrics", None),
    "tai42_skeleton.routers.notifications": ("GET", "/api/notifications", None),
    "tai42_skeleton.routers.observability": ("GET", "/api/observability/runs", None),
    "tai42_skeleton.routers.presets": ("GET", "/api/presets", None),
    "tai42_skeleton.routers.resources": ("POST", "/api/resources/get", {}),
    "tai42_skeleton.routers.schedules": ("GET", "/api/schedules", None),
    "tai42_skeleton.routers.storage": ("GET", "/api/storage", None),
    "tai42_skeleton.routers.sub_mcp": ("GET", "/api/sub-mcp", None),
    "tai42_skeleton.routers.system_kinds": ("GET", "/api/system/kinds", None),
    "tai42_skeleton.routers.templates": ("GET", "/api/templates", None),
    # The default-router stack registers the toolbox ``generate_uuid`` tool, so the
    # tool-extensions door has a known tool to answer for (an unknown tool 404s at the
    # operation, not the route).
    "tai42_skeleton.routers.tool_extensions": ("GET", "/api/tools/generate_uuid/extensions", None),
    "tai42_skeleton.routers.tool_meta": ("GET", "/api/tool-meta", None),
    "tai42_skeleton.routers.tool_runs": ("GET", "/api/tool-runs", None),
    "tai42_skeleton.routers.tools": ("GET", "/api/tools", None),
}


async def _status(stack: TaiStack, method: str, path: str, json: dict | None) -> httpx.Response:
    """Issue one raw request against the app port, retrying only past the boot-time
    reload gate (a 503 while a worker self-resyncs is not a route verdict)."""
    api = stack.api()

    async def settled() -> httpx.Response | None:
        resp = await api.request_raw(method, path, json=json)
        if resp.status_code == 503:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("reloading") is True:
                return None
        return resp

    return await wait_for_async(settled, deadline=15.0, message=f"{method} {path} never left the reload gate")


def test_anchor_set_matches_default_routers() -> None:
    """SECONDARY completeness cross-check: the hand-pinned anchor set covers exactly
    ``DEFAULT_API_ROUTERS`` — no default router without an anchor (would go untested),
    no anchor for a non-default router (would test a route the default set does not
    promise). A drift here is a STOP: reconcile the anchor table with the skeleton's
    default set before trusting the coverage assertions."""
    anchors = set(STUDIO_ROUTE_ANCHORS)
    defaults = set(DEFAULT_API_ROUTERS)
    missing = defaults - anchors
    extra = anchors - defaults
    assert not missing, f"default routers with no coverage anchor: {sorted(missing)}"
    assert not extra, f"coverage anchors for non-default routers: {sorted(extra)}"


@pytest.mark.parametrize(
    ("router", "method", "path", "json"),
    [(router, method, path, json) for router, (method, path, json) in STUDIO_ROUTE_ANCHORS.items()],
    ids=list(STUDIO_ROUTE_ANCHORS),
)
async def test_default_manifest_mounts_studio_route(
    default_router_stack: TaiStack, router: str, method: str, path: str, json: dict | None
) -> None:
    """PRIMARY independent guard: on the DEFAULT manifest, every Studio feature route
    answers non-404. A 404 means the router went dark — a Studio feature route dropped
    from the default manifest."""
    resp = await _status(default_router_stack, method, path, json)
    assert resp.status_code != 404, (
        f"{router} is dark: {method} {path} -> 404 on the default manifest; body: {resp.text}"
    )


async def test_catch_all_does_not_shadow_the_api(default_router_stack: TaiStack) -> None:
    """The SPA catch-all is mounted LAST and guards ``/api`` paths, so an unknown API
    path is still a genuine 404 (the catch-all did not swallow it) and the SPA root
    404s cleanly with no dist configured (never a 5xx)."""
    unknown = await _status(default_router_stack, "GET", "/api/this-route-does-not-exist", None)
    assert unknown.status_code == 404, f"unknown /api path was not a clean 404: {unknown.status_code}"

    root = await _status(default_router_stack, "GET", "/", None)
    assert root.status_code in (200, 404), f"SPA root should serve or 404 cleanly, got {root.status_code}"
