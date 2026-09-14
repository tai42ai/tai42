"""Effective router-set composition and the manifest-aware shared router importer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.route_defaults import CORE_API_ROUTERS, DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER
from tai42_skeleton.manifest import Manifest

from ._doubles import _Mixin


def _effective(default_routers=None, routers_modules=None) -> list[str]:
    m = _Mixin()
    body: dict[str, Any] = {}
    if default_routers is not None:
        body["default_routers"] = default_routers
    if routers_modules is not None:
        body["routers_modules"] = routers_modules
    m._manifest = Manifest.model_validate(body)
    return m._effective_router_modules()


def test_all_with_empty_list_is_core_then_defaults_then_catch_all_last():
    # "all" is the default when default_routers is omitted. The core tier leads,
    # then the default API routers, then the catch-all last.
    eff = _effective(routers_modules=[])
    assert eff == [*CORE_API_ROUTERS, *DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER]
    assert eff[-1] == STUDIO_SPA_ROUTER


def test_all_with_extra_appends_extra_before_catch_all():
    eff = _effective(default_routers="all", routers_modules=["some.extra.router"])
    assert eff == [*CORE_API_ROUTERS, *DEFAULT_API_ROUTERS, "some.extra.router", STUDIO_SPA_ROUTER]


def test_all_dedups_a_redundantly_listed_core_router_no_double_mount():
    # A manifest that still lists a defaulted core router imports it exactly once.
    core = DEFAULT_API_ROUTERS[0]
    eff = _effective(default_routers="all", routers_modules=[core])
    assert eff.count(core) == 1
    assert eff[-1] == STUDIO_SPA_ROUTER
    assert eff == [*CORE_API_ROUTERS, *DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER]


def test_all_never_double_appends_an_explicitly_listed_catch_all():
    # An operator listing the catch-all among extras under "all" gets it exactly
    # once, still last — never in the middle, never twice.
    eff = _effective(default_routers="all", routers_modules=[STUDIO_SPA_ROUTER])
    assert eff.count(STUDIO_SPA_ROUTER) == 1
    assert eff == [*CORE_API_ROUTERS, *DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER]


def test_api_mounts_core_and_defaults_without_catch_all():
    eff = _effective(default_routers="api", routers_modules=["some.extra.router"])
    assert eff == [*CORE_API_ROUTERS, *DEFAULT_API_ROUTERS, "some.extra.router"]
    assert STUDIO_SPA_ROUTER not in eff


def test_api_honors_an_explicitly_listed_catch_all_last():
    # Under "api" the loader never adds the catch-all, but an operator who lists it
    # explicitly is honored — placed last.
    eff = _effective(default_routers="api", routers_modules=[STUDIO_SPA_ROUTER])
    assert eff == [*CORE_API_ROUTERS, *DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER]


def test_none_is_the_core_tier_then_the_verbatim_manual_surface_with_catch_all_last():
    eff = _effective(
        default_routers="none",
        routers_modules=["a.router", STUDIO_SPA_ROUTER, "b.router"],
    )
    # No defaults; the core tier still leads, the operator list is authoritative
    # after it, catch-all moved to last.
    assert eff == [*CORE_API_ROUTERS, "a.router", "b.router", STUDIO_SPA_ROUTER]


def test_none_with_empty_list_is_the_core_tier_only():
    # Even the leanest boot mounts the always-on core tier.
    assert _effective(default_routers="none", routers_modules=[]) == [*CORE_API_ROUTERS]


def test_effective_router_modules_requires_started():
    m = _Mixin()  # _manifest is None
    with pytest.raises(RuntimeError, match="not started"):
        m._effective_router_modules()


def test_default_boot_mounts_the_studio_and_cli_page_routes():
    """A DEFAULT ("all") boot registers the specific Studio/CLI-consumed endpoints.
    Asserted by LITERAL path (not derived from DEFAULT_API_ROUTERS) against the
    routes the boot registered, so the composition is checked against real
    materialized routes a partial router list is prone to omit."""
    manifest = Manifest.model_validate({"default_routers": "all"})

    async def run():
        async with app.app_context(manifest):
            routes = app._fast_mcp._additional_http_routes
            registered = {getattr(route, "path", None) for route in routes}
            # channels backs the Interactions ChannelsCard, sub-mcp the Manifest
            # SubMcpTab, resources/get the `tai resources get` CLI.
            assert "/api/channels" in registered
            assert "/api/sub-mcp" in registered
            assert "/api/resources/get" in registered
            # A representative privileged page a partial manifest could omit.
            assert "/api/marketplace/install" in registered
            assert "/api/backup/export" in registered
            # The SPA catch-all is mounted AND is the LAST-registered route — its
            # ``/{spa_path:path}`` matches any path, so anything registered after it
            # would be shadowed. Assert its terminal position, not mere membership.
            assert getattr(routes[-1], "path", None) == "/{spa_path:path}"

    asyncio.run(run())


def test_started_none_boot_serves_only_the_curated_routers(monkeypatch):
    """Regression: an access-control-enabled boot with ``default_routers="none"`` and a
    curated two-module ``routers_modules`` must serve ONLY those modules' routes. The
    access-control startup audits enumerate the surface through ``load_all_routes``/``load_api_routes``;
    that shared importer must NOT pull the whole ``tai42_skeleton.routers`` package into the
    started app's live route table — doing so silently serves every router the manifest
    excluded.

    Asserted on the PER-INSTANCE served table of a FRESH app: the process-global
    ``route_registry`` is legitimately polluted by other tests, and the singleton's served
    table accumulates across boots. ``marketplace``/``connectors`` are popped from
    ``sys.modules`` first, so the whole-package importer — if it ran — WOULD re-execute
    their ``@custom_route`` decorators into this app's live table; this test asserts that importer does not run."""
    import sys

    from tai42_skeleton.access_control.startup import (
        check_accounts_providers_configured,
        check_always_public_routes,
        check_fenced_routes_resolvable,
        check_route_actions,
        check_spa_shell_public,
        probe_identity_provider,
        seed_roles,
    )
    from tai42_skeleton.app.server import TaiMCP

    instance = TaiMCP(name="curation-under-test")
    # Wire the access-control startup audits exactly as the real build does when access
    # control is enabled, so every audit's route enumeration runs during this boot.
    for audit in (
        probe_identity_provider,
        seed_roles,
        check_always_public_routes,
        check_spa_shell_public,
        check_route_actions,
        check_fenced_routes_resolvable,
        check_accounts_providers_configured,
    ):
        instance.lifecycle.on_startup(audit)

    excluded = ("tai42_skeleton.routers.marketplace", "tai42_skeleton.routers.connectors")
    saved_modules = {name: sys.modules.pop(name, None) for name in excluded}

    manifest = Manifest.model_validate(
        {
            "default_routers": "none",
            "routers_modules": ["tai42_skeleton.routers.tools", "tai42_skeleton.routers.health"],
        }
    )

    async def run() -> set[str | None]:
        async with instance.app_context(manifest):
            return {getattr(route, "path", None) for route in instance._fast_mcp._additional_http_routes}

    try:
        # ``app_context`` binds the instance under test; scoping that bind restores
        # whatever this process had bound when the boot is over.
        with tai42_app.bound(None):
            served = asyncio.run(run())
    finally:
        for name, module in saved_modules.items():
            if module is not None:
                sys.modules[name] = module

    # The curated modules' routes ARE served ...
    assert "/api/tools" in served
    assert "/health" in served
    # ... and NOTHING from the excluded routers reached the live route table.
    assert not any((path or "").startswith("/api/marketplace") for path in served)
    assert not any((path or "").startswith("/api/connectors") for path in served)


def test_load_all_routes_uses_the_effective_set_when_started_and_whole_package_offline(monkeypatch):
    """The shared importer chooses its universe: a bound+STARTED app enumerates its
    manifest's effective router set and must NEVER fall back to the whole-package importer;
    an unbound (offline spec-harness) process MUST use it."""
    from tai42_skeleton.app import route_registry as rr

    # Started case: the whole-package importer must not run — the effective set is the
    # universe. Patched inside the context so start()'s own router import is untouched.
    def _forbidden() -> None:
        raise AssertionError("_import_all_router_modules must not run in a started process")

    manifest = Manifest.model_validate({"default_routers": "none", "routers_modules": ["tai42_skeleton.routers.tools"]})

    async def run_started() -> None:
        async with app.app_context(manifest):
            monkeypatch.setattr(rr, "_import_all_router_modules", _forbidden)
            rr.load_all_routes()  # no raise: the effective set is enumerated directly

    asyncio.run(run_started())

    # Offline case: with no deployment bound, the whole-package importer MUST run.
    calls: list[int] = []
    monkeypatch.setattr(rr, "_import_all_router_modules", lambda: calls.append(1))
    # Unbound for the call: no started deployment answers effective_router_modules().
    with tai42_app.bound(None):
        rr.load_all_routes()
    assert calls == [1]


def test_load_all_routes_leaves_the_bound_app_exactly_as_it_found_it(monkeypatch):
    """The enumeration is a READ: it must never replace the process's app binding.

    A bound impl that answers no router universe — a partially-faked app, a harness
    stand-in — takes the offline branch, whose router import runs under the ``_SpecApp``
    stand-in for that import alone. Overwriting the binding instead would strand
    whichever component owns the real one, and an unbound process must come back
    unbound rather than silently acquiring a spec app.
    """
    from types import SimpleNamespace

    from tai42_skeleton.app import route_registry as rr

    imports: list[str] = []

    def _record_universe_app() -> None:
        # The offline import needs a stand-in exposing ``http``; record which impl is
        # bound while it runs.
        imports.append(type(tai42_app.http._app).__name__)  # pyright: ignore[reportAttributeAccessIssue]

    monkeypatch.setattr(rr, "_import_all_router_modules", _record_universe_app)

    partial = SimpleNamespace(storage="fake-storage")
    with tai42_app.bound(partial):
        rr.load_all_routes()
        assert tai42_app.storage == "fake-storage"

    with tai42_app.bound(None):
        rr.load_all_routes()
        with pytest.raises(AttributeError, match="accessed before bind"):
            _ = tai42_app.storage

    assert imports == ["_SpecApp", "_SpecApp"]
