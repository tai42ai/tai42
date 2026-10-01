"""Compose the effective router set and the module-to-mount-binding map for a registration pass."""

from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.mount_map import MountBinding, build_mount_map
from tai42_skeleton.app.route_defaults import effective_router_modules


class RouterModulesMixin(LifecycleState):
    """Compose the router set and mount-binding map for a registration pass."""

    def _effective_router_modules(self) -> list[str]:
        """The router modules to import this boot, composed from the started manifest.

        Recomputed fresh on every start()/reload so the composition holds across
        reloads. Delegates to :func:`~tai42_skeleton.app.route_defaults.effective_router_modules`
        so the serving importer and offline chain discovery share one composition.
        """
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        return effective_router_modules(self._manifest)

    def effective_router_modules(self) -> list[str] | None:
        """The started manifest's effective router set, or None before start()."""
        if self._manifest is None:
            return None
        return self._effective_router_modules()

    def _build_mount_map(self) -> dict[str, MountBinding]:
        """The module → :class:`MountBinding` map for this registration pass.

        Built from the manifest's channel/router modules' packaged ``tai-plugin.yml``
        with persisted route-mount overrides applied. The install store is read ONLY
        when a real plugin route module is present. A resolved public route under a
        reserved never-public prefix fails the build.
        """
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        from tai42_skeleton.access_control.settings import access_control_settings

        modules = [*(self._manifest.channel_modules or []), *self._effective_router_modules()]
        reserved = access_control_settings().reserved_public_pin_prefixes
        return build_mount_map(modules, reserved, self._installed_route_mounts)

    def _installed_route_mounts(self) -> dict[str, dict[str, str]]:
        """Every marketplace-install row's ``{ref: {item_name: base}}`` route-mount overrides.

        The persisted operator remaps the boot mount-map reproduces. Empty when the
        skeleton database is not configured (a file-mode deployment has no rows). Read
        off-loop so a sync/serving caller both resolve it.
        """
        from tai42_kit.db import component_store_configured

        from tai42_skeleton.db import SKELETON_COMPONENT
        from tai42_skeleton.marketplace.store import MarketplaceInstallStore

        if not component_store_configured(SKELETON_COMPONENT):
            return {}
        records = run_blocking(lambda: MarketplaceInstallStore().list_installed())
        return {record.ref: dict(record.route_mounts) for record in records}
