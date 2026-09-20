"""The router-composition stack profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
from tai42_e2e.manifests.marketplace import build_marketplace_stack
from tai42_e2e.manifests.tool_entries import _toolbox_tools_entry
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The Studio SPA catch-all router. Its ``/{path}`` route matches every path, so it must
# import LAST — any router after it serves nothing. The marketplace merge inserts a plugin
# router before it.
_STUDIO_SPA_ROUTER = "tai42_skeleton.routers.plugins"


def build_router_merge_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace stack with the SPA catch-all router LAST — the home of the
    plugin-router/middleware auto-merge spec.

    ``default_routers="none"`` with an explicit ``routers_modules`` (the marketplace core
    surface plus the SPA catch-all last) so installing a router-providing fixture plugin
    exercises the ordering-aware merge: the installer inserts the plugin's router module
    immediately before ``tai42_skeleton.routers.plugins``, and a restart mounts it there —
    reachable because it precedes the catch-all. Same shape as the marketplace stack it
    derives from."""
    from dataclasses import replace

    base = build_marketplace_stack(res, variants)
    manifest = {**base.manifest, "default_routers": "none"}
    manifest["routers_modules"] = [*base.manifest["routers_modules"], _STUDIO_SPA_ROUTER]
    return replace(base, name="router-merge", manifest=manifest)


def build_default_router_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — boots on the DEFAULT router set
    (``default_routers="all"`` with no ``routers_modules``) so the route-coverage
    guard can assert every Studio feature route the skeleton default-mounts answers
    non-404.

    The manifest names no routers of its own: the loader mounts ``DEFAULT_API_ROUTERS``
    plus the SPA catch-all last, as a bare full-Studio deployment does. It carries the
    toolbox ``generate_uuid`` tool purely so the tool-extensions door has a tool to answer
    for; ``api_tools`` off. No backend/storage/metrics — the doors for absent providers
    still mount and answer non-404, which is the point. Auth off."""
    manifest = {
        "default_routers": "all",
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
    }
    return StackConfig(
        name="default-router",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_api_router_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the HEADLESS default set (``default_routers="api"``
    with no ``routers_modules``): the loader mounts ``DEFAULT_API_ROUTERS`` but NOT the
    SPA catch-all, so ``/api/*`` answers while ``/`` (and any client path) has no SPA
    shell to serve. The boot test asserts that contrast against the ``"all"`` and
    ``"none"`` modes.

    Same bare shape as ``build_default_router_stack`` — only ``default_routers`` differs,
    the single variable under test."""
    manifest = {
        "default_routers": "api",
        "tools": [_toolbox_tools_entry()],
        "api_tools": {"enabled": False},
    }
    return StackConfig(
        name="api-router",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=_base_env(res, variants),
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
