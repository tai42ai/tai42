"""The lifecycle/admin/config/sub-app facades."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from tai42_contract.config import ConfigManager
    from tai42_contract.sub_mcp import SubMcpAppRouter

    from tai42_skeleton.manifest import Manifest as ManifestImpl


class LifecycleFacet(_Facet):
    """``app.lifecycle`` — startup/shutdown/reload handler registration (``AppLifecycle``)."""

    def on_startup(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register ``func`` to run at startup."""
        return self._app._on_startup(func)

    def on_shutdown(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register ``func`` to run at shutdown."""
        return self._app._on_shutdown(func)

    def on_reload(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register ``func`` to run on reload."""
        return self._app._on_reload(func)

    def on_post_swap(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register an establisher for a loop-affine background loop.

        Run on the serving loop at boot and after every epoch swap — never on
        the throwaway build-thread loop the per-epoch handlers run on.
        """
        return self._app._on_post_swap(func)

    def on_fleet_op_applied(self, func: Callable[[str], Any]) -> Callable[[str], Any]:
        """Register ``func`` to run after a fleet op is applied, given the op name."""
        return self._app._on_fleet_op_applied(func)

    def reload_registries(self, manifest: ManifestImpl) -> dict[str, Any]:
        """Re-initialise the registries from ``manifest`` and run the per-epoch handler list once.

        The rebuild step the epoch build+swap primitive calls.
        """
        return self._app._reload_registries(manifest)

    async def wait_until_ready(self) -> None:
        """Block until the app has finished starting up."""
        await self._app._wait_until_ready()

    def read_boot_manifest(self) -> ManifestImpl:
        """Bridge the persisted env store into ``os.environ`` and read + validate the boot manifest under it.

        Names any still-dangling ``!ENV`` marker — the one cold-boot manifest
        read every serving/backend entrypoint crosses.
        """
        return self._app._read_boot_manifest()


class AdminFacet(_Facet):
    """``app.admin`` — runtime management surface (``AppAdmin``)."""

    def reload_mcp(self, title: str) -> dict[str, Any]:
        """Reload the sub-MCP app titled ``title`` and report the outcome."""
        return self._app._reload_mcp(title)

    def deregister_mcp(self, title: str) -> dict[str, Any]:
        """Deregister the sub-MCP app titled ``title`` and report the outcome."""
        return self._app._deregister_mcp(title)

    def reload_config(self) -> dict[str, Any]:
        """Reload the process config in place and report the outcome."""
        return self._app._reload_config()

    def tool_reloader(self, kind: str) -> Callable[..., Any]:
        """Return the reload callable for tools of the given ``kind``."""
        return self._app._tool_reloader(kind)

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        """Run a tool reload ``action`` for ``name`` of ``kind`` and report the outcome."""
        return await self._app._run_tool_reload(kind, action, name)

    def reload_failed_mcps(self) -> list[dict[str, Any]]:
        """Retry every sub-MCP app that failed to load and report each outcome."""
        return self._app._reload_failed_mcps()

    def list_failed_mcps(self) -> list[dict[str, Any]]:
        """List the sub-MCP apps that failed to load."""
        return self._app._list_failed_mcps()

    def live_mcp_status(self) -> dict[str, Any]:
        """Report the live status of every registered sub-MCP app."""
        return self._app._live_mcp_status()

    @property
    def live_manifest(self) -> dict[str, Any]:
        """The live in-process manifest as an emitted, model-dumped dict."""
        return self._app._live_manifest

    @property
    def live_manifest_typed(self) -> ManifestImpl:
        """The live in-process manifest as the skeleton ``Manifest``, raising if the app is not started.

        Carries the resolved selection maps + predicates — the typed companion
        to :attr:`live_manifest` (the emitted, model-dumped dict). This is the
        shared live object, not a copy (the predicates and resolved maps
        are the point; a copy would lose them). Read-only: callers must not mutate it —
        an edit belongs on a fresh ``config_manager.read_manifest()`` dict, never here.
        """
        return self._app._require_live_manifest()


class ConfigFacet(_Facet):
    """``app.config`` — the active config manager (``AppConfig``)."""

    @property
    def config_manager(self) -> ConfigManager:
        """The active config manager."""
        return self._app._config_manager


class SubAppFacet(_Facet):
    """``app.sub_app`` — the live sub-MCP app router (``AppSubApp``)."""

    @property
    def mcp_sub_app_router(self) -> SubMcpAppRouter:
        """The live sub-MCP app router."""
        return self._app._mcp_sub_app_router
