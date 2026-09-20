"""Reload-handler rerun on the in-place ``update()`` path.

``update()`` re-runs the registered reload handlers under ``raise_on_error``, so a
config reload re-applies every reload-time side effect and a failing handler fails
the op loudly rather than leaving the worker silently behind.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest
from tai42_contract.plugins import RouteDecl

from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.server import ServingCore
from tai42_skeleton.manifest import Manifest

_PLUGIN_DIST_ROOT = Path(__file__).parent.parent / "app" / "_fixtures" / "_plugin_dist"


class _Mixin(TaiMCPLifecycleMixin):
    """Concrete-enough subclass to exercise update()'s handler re-run without
    an event server, network, or the full app: start() is stubbed to skip the
    re-init and there are no live tools to drop."""

    def __init__(self):
        super().__init__()
        self.started_with = None
        self._building = ServingCore(cast("object", self), args=(), auth=None, kwargs={"name": "reload-under-test"})  # type: ignore[arg-type]

    def _mcp_tools(self, config, tools):  # abstract in the mixin
        pass

    def start(self, manifest):
        self.started_with = manifest


def test_reload_registries_reruns_handlers() -> None:
    mixin = _Mixin()
    ran = []

    @mixin._on_reload
    async def _reload_marker() -> None:
        ran.append("reload")

    manifest = Manifest()
    mixin._reload_registries(manifest)

    assert mixin.started_with is manifest
    assert ran == ["reload"]


def test_reload_registries_raises_when_handler_fails() -> None:
    # raise_on_error on the per-epoch rebuild: a failed reload handler must fail the
    # build loudly (so the primitive discards the half-built epoch), never leave the
    # worker silently behind.
    mixin = _Mixin()

    @mixin._on_reload
    async def _boom() -> None:
        raise RuntimeError("reload blew up")

    with pytest.raises(RuntimeError, match="lifecycle handlers failed"):
        mixin._reload_registries(Manifest())


def _forget(prefixes: tuple[str, ...]) -> None:
    for name in [n for n in sys.modules if any(n == p or n.startswith(f"{p}.") for p in prefixes)]:
        del sys.modules[name]


def test_reload_registries_audits_dropped_plugin_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The route-preservation audit is WIRED through the real _reload_registries seam:
    # when a still-declared route plugin loses every route in the staged generation, the
    # rebuild raises EpochRouteAuditError before the swap so the old epoch keeps serving.
    # Drives the REAL _reload_registries; the sibling-cache bug is injected at the
    # narrowest realistic seam — start() re-imports the leaf ALONE (bypassing the
    # recorded sibling extra), so the cached sibling never re-fires.
    from tai42_skeleton.app import route_registry as rr_module
    from tai42_skeleton.app.http import plugin_owner
    from tai42_skeleton.app.importer import import_or_reload_package
    from tai42_skeleton.app.mount_map import MountBinding, MountRegistrationError, bind_module
    from tai42_skeleton.app.route_registry import EpochRouteAuditError, RouteRegistry

    leaf = "route_sibling_plugin.register"
    served_path = "/api/channels/web/inbound"
    binding = MountBinding(
        owner_ref="fixture/web",
        item_name="web",
        base="channels/web",
        declared_routes=(RouteDecl(path="/inbound", methods=["POST"], public=True),),
    )

    registry = RouteRegistry()
    monkeypatch.setattr(rr_module, "route_registry", registry)
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.route_registry", registry)
    monkeypatch.syspath_prepend(str(_PLUGIN_DIST_ROOT))
    prefixes = ("route_sibling_plugin", "_regprobe")
    _forget(prefixes)
    try:
        # Boot: register the plugin's route into the COMMITTED (live epoch) generation.
        with bind_module(binding):
            import_or_reload_package(leaf)
        assert registry.match(served_path, "POST") is not None

        # Open a staged generation, as the epoch build+swap primitive does before rebuild.
        registry.begin_shape_staging()

        mixin = _Mixin()
        mixin._mount_map = {leaf: binding}

        # Inject the sibling-cache bug at the reimport seam: re-import the leaf ALONE, so
        # the cached sibling never re-fires. The bind-time completeness check then raises,
        # which _import_additive_plugin catches and quarantines — rolling the plugin's
        # rows out of the STAGED generation, exactly the drop the audit must catch.
        def buggy_start(manifest: Manifest) -> None:
            registry.reset_shape_index()  # clears the STAGED target, as the real start() does
            try:
                with bind_module(binding):
                    import_or_reload_package(leaf)  # no sibling extra → route never re-registers
            except MountRegistrationError:
                registry.rollback_owner(plugin_owner(binding))  # quarantine drops the partial rows

        monkeypatch.setattr(mixin, "start", buggy_start)

        assert registry.match(served_path, "POST") is not None  # committed still live pre-audit
        with pytest.raises(EpochRouteAuditError, match="fixture/web:web"):
            mixin._reload_registries(Manifest())
    finally:
        _forget(prefixes)
