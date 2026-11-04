"""The lifecycle/admin/config/sub-app facades."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from tai42_skeleton.app import admission
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.reload_gate import recorded_wait

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from typing import Any

    from tai42_contract.backend import FleetOpApplied
    from tai42_contract.config import ConfigManager
    from tai42_contract.manifest import TaiMCPConfig

    from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
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

        Run on the serving loop at boot and after every epoch swap — never by the
        per-epoch handlers, which run while the generation is still being built.
        """
        return self._app._on_post_swap(func)

    def on_fleet_op_applied(self, func: Callable[[FleetOpApplied], Any]) -> Callable[[FleetOpApplied], Any]:
        """Register ``func`` to run after a fleet op is applied, given the applied op with its effects and budget."""
        return self._app._on_fleet_op_applied(func)

    async def reload_registries(self, manifest: ManifestImpl) -> dict[str, Any]:
        """Re-initialise the registries from ``manifest`` and run the per-epoch handler list once.

        The rebuild step the epoch build+swap primitive calls.
        """
        return await self._app._reload_registries(manifest)

    async def wait_until_ready(self) -> None:
        """Block until the app has finished starting up."""
        await self._app._wait_until_ready()

    @asynccontextmanager
    async def waiting_on_fleet_work(self) -> AsyncIterator[None]:
        """Span a wait on work outside this process as a recorded wait of the current admitted work or run."""
        async with recorded_wait():
            yield

    @contextmanager
    def admitted_job(self) -> Iterator[None]:
        """Admit the enclosed backend job's run on the live serving generation while it runs on the serving loop."""
        with admission.admitted_job():
            yield

    async def read_boot_manifest(self, *, kind: WorkerKind = WorkerKind.serve) -> ManifestImpl:
        """Build the worker bus, bridge the persisted env store into ``os.environ``, read + validate the boot manifest.

        ``kind`` is the worker kind this process joins the bus as. Names any still-dangling
        ``!ENV`` marker — the one cold-boot manifest read every serving/backend entrypoint
        crosses, and the call that must precede ``app_context``.
        """
        return await self._app._read_boot_manifest(kind=kind)


class AdminFacet(_Facet):
    """``app.admin`` — runtime management surface (``AppAdmin``)."""

    async def reload_mcp(self, title: str) -> dict[str, Any]:
        """Reload the sub-MCP app titled ``title`` and report the outcome."""
        return await self._app._reload_mcp(title)

    async def deregister_mcp(self, title: str) -> dict[str, Any]:
        """Deregister the sub-MCP app titled ``title`` and report the outcome."""
        return await self._app._deregister_mcp(title)

    async def reload_config(self) -> dict[str, Any]:
        """Reload the process config in place and report the outcome."""
        return await self._app._reload_config()

    def tool_reloader(self, kind: str) -> Callable[..., Any]:
        """Return the reload callable for tools of the given ``kind``."""
        return self._app._tool_reloader(kind)

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        """Run a tool reload ``action`` for ``name`` of ``kind`` and report the outcome."""
        return await self._app._run_tool_reload(kind, action, name)

    async def reload_failed_mcps(self) -> list[dict[str, Any]]:
        """Retry every sub-MCP app that failed to load and report each outcome."""
        return await self._app._reload_failed_mcps()

    async def reprobe_failed_mcps(self, titles: list[str]) -> list[dict[str, Any]]:
        """Re-probe those of ``titles`` that failed to load, bind the recovered ones, and report each outcome.

        Probes off the reload gate, waiting the whole start budget, and takes the gate only to bind.
        """
        return await self._app._reprobe_failed_titles(titles)

    async def advertised_mcp_tools(self, title: str) -> list[dict[str, Any]]:
        """List every tool in the listing this worker bound MCP server ``title`` from, before the selection.

        Each row says why a selected tool is not bound on this worker (``unbound_reason``). Never
        starts the server. Raises ``LookupError`` for a title not in the live manifest, and
        :class:`~tai42_contract.manifest.errors.McpListingUnavailableError` when this worker holds no
        listing for the title.
        """
        return await self._app._advertised_mcp_tools(title)

    async def served_mcp_names(self, config: TaiMCPConfig) -> list[str]:
        """The names the binder would bind from the listing this worker bound ``config``'s server from.

        Read before the selection; a listed tool whose input schema cannot be adapted is not served.
        Never starts the server; raises
        :class:`~tai42_contract.manifest.errors.McpListingUnavailableError` when this worker holds no
        listing for the title.
        """
        return await self._app._served_mcp_names(config)

    def list_failed_mcps(self) -> list[dict[str, Any]]:
        """List the sub-MCP apps that failed to load."""
        return self._app._list_failed_mcps()

    def live_mcp_status(self) -> dict[str, Any]:
        """Report the live status of every registered sub-MCP app."""
        return self._app._live_mcp_status()

    @property
    def live_manifest(self) -> dict[str, Any]:
        """The live serving generation's manifest as an emitted, model-dumped dict.

        A request read: while a rebuild is in flight it answers the generation still serving,
        never the one under construction.
        """
        return self._app._live_manifest

    @property
    def live_manifest_typed(self) -> ManifestImpl:
        """The live serving generation's manifest as the skeleton ``Manifest``, raising if the app is not started.

        A request read like :attr:`live_manifest`: a rebuild in flight does not change it.

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
