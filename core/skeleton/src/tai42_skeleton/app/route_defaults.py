"""The routers the skeleton mounts by default.

``CORE_API_ROUTERS`` is the always-on tier the skeleton mounts on EVERY boot;
``DEFAULT_API_ROUTERS`` is the ordered set of every OTHER route-registering API
router module the skeleton mounts without an operator naming it; and
``STUDIO_SPA_ROUTER`` is the Studio SPA catch-all that must import LAST — it
matches ``/{spa_path:path}`` and would shadow any router registered after it.

``Manifest.default_routers`` selects how the default tier composes:

- ``"all"`` — mount ``DEFAULT_API_ROUTERS``, then the manifest's own extras,
  then ``STUDIO_SPA_ROUTER`` last (the full-Studio deployment).
- ``"api"`` — mount ``DEFAULT_API_ROUTERS`` plus extras but NOT the SPA
  catch-all (a headless JSON ``/api`` deployment, no browser UI).
- ``"none"`` — mount nothing by default; ``routers_modules`` is authoritative
  (a fully-manual or MCP-only surface).

``CORE_API_ROUTERS`` is independent of that selector — it is force-mounted at the
composition chokepoint (``lifecycle.py::_effective_router_modules``) under every
value, so a deployment-invariant answer is reachable even in a ``"none"`` boot.

Membership of the two tiers is EVERY module under ``tai42_skeleton.routers`` that
registers an HTTP route, EXCEPT the SPA catch-all (force-appended last, never in
either tuple) and the route-less helper modules (``_tool_call``,
``metrics_settings``, ``observability_support``, ``tool_runs_settings``,
``prometheus``). ``tests/app/test_route_defaults.py`` re-derives that set by
iterating the real package and asserts the union of ``DEFAULT_API_ROUTERS``,
``CORE_API_ROUTERS`` and {``STUDIO_SPA_ROUTER``} equals it, so a newly-added
route-registering router missing from both tuples fails the test rather than being
silently un-mounted; the two tuples are disjoint.
"""

from __future__ import annotations

# The Studio SPA catch-all. It registers ``GET /{spa_path:path}`` (matches any
# path) so it must import after every API router, else it shadows them. The
# loader force-appends it LAST under ``"all"``; the ordering-aware manifest patch
# inserts plugin routers BEFORE it. This is the ONE place the module path is
# spelled — every other consumer imports this constant.
STUDIO_SPA_ROUTER = "tai42_skeleton.routers.plugins"

# The always-on core tier: the modules the skeleton mounts on EVERY boot
# regardless of ``default_routers``/``routers_modules``, for a deployment-invariant
# answer that must be reachable even where no management surface is mounted. The
# presence read ``GET /api/storage`` ("is a storage provider installed?") lives
# here so it survives a lean ``"none"`` boot. Disjoint from ``DEFAULT_API_ROUTERS``
# and never contains the SPA catch-all.
CORE_API_ROUTERS: tuple[str, ...] = ("tai42_skeleton.routers.storage_presence",)

# The 34 route-registering API router modules mounted by default under
# ``"all"``/``"api"``. Ordered alphabetically; among these each owns a distinct
# ``/api/*`` (or ``/health``/``/ready``/``/metrics``) prefix, so their relative order is
# not load-bearing — only the SPA catch-all's last position is.
DEFAULT_API_ROUTERS: tuple[str, ...] = (
    "tai42_skeleton.routers.agents",
    "tai42_skeleton.routers.api_keys",
    "tai42_skeleton.routers.backend",
    "tai42_skeleton.routers.backup",
    "tai42_skeleton.routers.channels",
    "tai42_skeleton.routers.checkpoints",
    "tai42_skeleton.routers.config",
    "tai42_skeleton.routers.connectors",
    "tai42_skeleton.routers.conversations",
    "tai42_skeleton.routers.extensions",
    "tai42_skeleton.routers.health",
    "tai42_skeleton.routers.hooks",
    "tai42_skeleton.routers.interactions",
    "tai42_skeleton.routers.keys_bootstrap",
    "tai42_skeleton.routers.login",
    "tai42_skeleton.routers.manifest",
    "tai42_skeleton.routers.marketplace",
    "tai42_skeleton.routers.metrics",
    "tai42_skeleton.routers.notifications",
    "tai42_skeleton.routers.observability",
    "tai42_skeleton.routers.presets",
    "tai42_skeleton.routers.resources",
    "tai42_skeleton.routers.runs",
    "tai42_skeleton.routers.sandbox",
    "tai42_skeleton.routers.schedules",
    "tai42_skeleton.routers.states",
    "tai42_skeleton.routers.storage",
    "tai42_skeleton.routers.sub_mcp",
    "tai42_skeleton.routers.system_kinds",
    "tai42_skeleton.routers.templates",
    "tai42_skeleton.routers.tool_extensions",
    "tai42_skeleton.routers.tool_meta",
    "tai42_skeleton.routers.tool_runs",
    "tai42_skeleton.routers.tools",
)
