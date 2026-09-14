"""Compose the effective router set and the module-to-mount-binding map for a registration pass."""

from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.mount_map import MountBinding, build_mount_map
from tai42_skeleton.app.route_defaults import CORE_API_ROUTERS, DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER


class RouterModulesMixin(LifecycleState):
    def _effective_router_modules(self) -> list[str]:
        """The router modules to import this boot, composed in three layers.

        - The always-on core tier (``CORE_API_ROUTERS``) leads, mounted regardless of
          ``default_routers``/``routers_modules`` so a deployment-invariant answer
          (storage presence) is reachable in every boot.
        - Then the manifest layer per ``default_routers``:
          - ``"all"``: the default API routers, then the manifest's extras, then the
            Studio SPA catch-all forced LAST.
          - ``"api"``: the default API routers, then extras, and NO SPA catch-all —
            unless the operator explicitly listed it (then it is honored last).
          - ``"none"``: no defaults; ``routers_modules`` is authoritative, with an
            operator-listed catch-all still moved to the end.

        Recomputed fresh on every start()/reload so the composition holds across
        reloads. The ``module not in effective`` check is the no-double-mount guard:
        a manifest that still lists a defaulted or core router imports it exactly once,
        so the catch-all is never accidentally un-lasted and no route registers
        twice. The ``module != STUDIO_SPA_ROUTER`` check keeps the catch-all out of
        the middle even when a manifest lists it among the extras — it is only ever
        appended by the two branches below, always last.
        """
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")

        listed = self._manifest.routers_modules or []
        effective: list[str] = list(CORE_API_ROUTERS)
        if self._manifest.default_routers != "none":
            effective.extend(module for module in DEFAULT_API_ROUTERS if module not in effective)
        for module in listed:
            if module not in effective and module != STUDIO_SPA_ROUTER:
                effective.append(module)
        # The catch-all goes last under "all" (the loader owns its placement) or
        # when an operator explicitly listed it under "api"/"none" (honored last).
        if self._manifest.default_routers == "all" or STUDIO_SPA_ROUTER in listed:
            effective.append(STUDIO_SPA_ROUTER)
        return effective

    def effective_router_modules(self) -> list[str] | None:
        """The started manifest's effective router set, or None before start()."""
        if self._manifest is None:
            return None
        return self._effective_router_modules()

    def _build_mount_map(self) -> dict[str, MountBinding]:
        """The module → :class:`MountBinding` map for this registration pass, built
        from the manifest's channel/router modules' packaged ``tai-plugin.yml`` with
        persisted route-mount overrides applied. The install store is read ONLY when a
        real plugin route module is present. A resolved public route under a reserved
        never-public prefix fails the build."""
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        from tai42_skeleton.access_control.settings import access_control_settings

        modules = [*(self._manifest.channel_modules or []), *self._effective_router_modules()]
        reserved = access_control_settings().reserved_public_pin_prefixes
        return build_mount_map(modules, reserved, self._installed_route_mounts)

    def _installed_route_mounts(self) -> dict[str, dict[str, str]]:
        """Every marketplace-install row's ``{ref: {item_name: base}}`` route-mount
        overrides — the persisted operator remaps the boot mount-map reproduces. Empty
        when the skeleton database is not configured (a file-mode deployment has no
        rows). Read off-loop so a sync/serving caller both resolve it."""
        from tai42_kit.db import component_store_configured

        from tai42_skeleton.db import SKELETON_COMPONENT
        from tai42_skeleton.marketplace.store import MarketplaceInstallStore

        if not component_store_configured(SKELETON_COMPONENT):
            return {}
        records = run_blocking(lambda: MarketplaceInstallStore().list_installed())
        return {record.ref: dict(record.route_mounts) for record in records}
