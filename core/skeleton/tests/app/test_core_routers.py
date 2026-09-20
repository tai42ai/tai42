"""The always-mounted core-router tier and the presence guarantee it carries.

``CORE_API_ROUTERS`` is force-mounted at the composition chokepoint
(``_effective_router_modules``) on every boot, independent of ``default_routers``
and ``routers_modules``. So the presence read ``GET /api/storage`` is answerable in
every deployment — including a lean ``default_routers: "none"`` boot that mounts no
storage management surface.
"""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app.route_defaults import CORE_API_ROUTERS, STUDIO_SPA_ROUTER
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.manifest import Manifest


def _effective(default_routers: str, routers_modules: list[str]) -> list[str]:
    # A constructed (not booted) app answers the composition directly from its manifest.
    inst = TaiMCP(name="core-router-composition")
    inst._manifest = Manifest.model_validate({"default_routers": default_routers, "routers_modules": routers_modules})
    return inst._effective_router_modules()


@pytest.mark.parametrize("default_routers", ["all", "api", "none"])
def test_core_routers_mounted_once_on_every_default_routers_value(default_routers: str) -> None:
    # A lean list that omits the storage management router entirely — the shape a
    # no-management deployment uses.
    eff = _effective(default_routers, ["tai42_skeleton.routers.tools"])
    for module in CORE_API_ROUTERS:
        assert eff.count(module) == 1


def test_core_routers_never_the_spa_catch_all() -> None:
    assert STUDIO_SPA_ROUTER not in CORE_API_ROUTERS


def test_spa_catch_all_stays_last_with_the_core_tier_present() -> None:
    eff = _effective("all", ["tai42_skeleton.routers.tools"])
    assert eff[-1] == STUDIO_SPA_ROUTER
    # Every core module sits ahead of the catch-all.
    for module in CORE_API_ROUTERS:
        assert eff.index(module) < eff.index(STUDIO_SPA_ROUTER)


def test_core_router_listed_by_the_manifest_mounts_exactly_once() -> None:
    core = CORE_API_ROUTERS[0]
    eff = _effective("none", [core, "tai42_skeleton.routers.tools"])
    assert eff.count(core) == 1


def test_presence_route_mounted_in_a_none_boot_without_the_management_surface() -> None:
    """A ``default_routers: "none"`` boot whose ``routers_modules`` omits the storage
    management router still serves ``GET /api/storage`` (core tier) and does NOT serve
    the management routes. Asserted on the served route table of a FRESH app — the
    process singleton's table accumulates across boots."""
    instance = TaiMCP(name="core-presence-under-test")
    manifest = Manifest.model_validate({"default_routers": "none", "routers_modules": ["tai42_skeleton.routers.tools"]})

    async def run() -> set[str | None]:
        async with instance.app_context(manifest):
            return {getattr(route, "path", None) for route in instance._fast_mcp._additional_http_routes}

    # Scope the bind so the process's own binding is restored afterwards.
    with tai42_app.bound(None):
        served = asyncio.run(run())

    assert "/api/storage" in served
    assert not any((path or "").startswith("/api/storage/resources") for path in served)
