"""Process runtime/admin facets: clients, lifecycle, admin, config, backup, sub-app, monitoring."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from tai42_contract.backup import BackupSectionInfo
from tai42_contract.clients import BaseClient
from tai42_contract.config import ConfigManager
from tai42_contract.monitoring import Monitoring
from tai42_contract.sub_mcp import SubMcpAppRouter


@runtime_checkable
class AppMonitoring(Protocol):
    """Registration and access for the process monitoring backend."""

    def register_monitoring(self, builder: Callable[..., Any] | None = None) -> Callable[..., Any]:
        """Register the monitoring backend builder, or return a decorator when ``builder`` is omitted."""
        ...

    @property
    def active(self) -> Monitoring:
        """The active monitoring backend (impl type behind the contract protocol)."""
        ...


@runtime_checkable
class AppClients(Protocol):
    """Pooled async client lifecycle for the process."""

    def client_ctx[ClientT](
        self,
        client_cls: type[BaseClient[ClientT]],
        settings: Any = None,
        *,
        fresh: bool = False,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[ClientT]:
        """Yield a connected client as an async context manager.

        Pooled per loop + connection params, or one-shot when ``fresh=True``.
        ``settings`` is a kit ``ClientSettings`` whose ``client_kwargs()`` supplies
        the connection key.
        """
        ...

    async def shutdown_clients(self) -> None:
        """Close every live client pool for the running loop."""
        ...


@runtime_checkable
class AppLifecycle(Protocol):
    """Startup, shutdown, reload, and post-swap lifecycle hook registration."""

    def on_startup(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register ``func`` to run at process startup (usable as a decorator)."""
        ...

    def on_shutdown(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register ``func`` to run at process shutdown (usable as a decorator)."""
        ...

    def on_reload(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register a handler re-run after every in-place re-init (``reload_config``).

        For example, dynamic tool loaders that ``on_startup`` ran once.
        """
        ...

    def on_post_swap(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register an establisher for a loop-affine background loop (a periodic poll or sweep).

        Run on the real serving loop at boot and after every epoch swap — never on
        the throwaway build-thread loop the per-epoch handlers run on — so the loop
        it spawns attaches to the serving loop and retires with its generation.
        """
        ...

    def on_fleet_op_applied(self, func: Callable[[str], Any]) -> Callable[[str], Any]:
        """Register a handler fired after any worker-bus op applies in this process.

        Also fired after the reconnect self-resync reload. Unlike its zero-arg
        siblings, the handler takes ONE argument — the op name — so it can act on
        some ops and skip others (a query op carries no state change to react to).
        """
        ...

    async def wait_until_ready(self) -> None:
        """Block until this process's first boot self-resync has completed.

        That is the point at which the tool registry is fully built and stable for
        the run. A backend runtime that forks a child (or otherwise consumes queued work)
        per job awaits this before its work loop accepts anything: the boot
        self-resync rebuilds the tool registry non-atomically, so a worker that
        dequeued and forked mid-rebuild would run the job against a half-built
        registry. The latch is one-way — it resolves once and stays resolved, so
        a later bus reconnect (which re-runs the self-resync while the app is
        already live) never un-readies the process.
        """
        ...


@runtime_checkable
class AppAdmin(Protocol):
    """In-process admin operations: MCP binding, tool reload, config reload."""

    def reload_mcp(self, title: str) -> dict[str, Any]:
        """Rebind the MCP server titled ``title`` and return its status."""
        ...

    def deregister_mcp(self, title: str) -> dict[str, Any]:
        """Remove the MCP server titled ``title`` and return its status."""
        ...

    def reload_config(self) -> dict[str, Any]:
        """Re-init the process config in place and return the reload report."""
        ...

    def tool_reloader(self, kind: str) -> Callable[..., Any]:
        """Register an async ``(action, name) -> dict`` reloader for ``kind``."""
        ...

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        """Run the registered ``kind`` reloader for ``action`` on ``name``."""
        ...

    def reload_failed_mcps(self) -> list[dict[str, Any]]:
        """Retry every MCP that failed to bind and return their statuses."""
        ...

    def list_failed_mcps(self) -> list[dict[str, Any]]:
        """List the MCPs that failed to bind."""
        ...

    def live_mcp_status(self) -> dict[str, Any]:
        """Snapshot the in-process MCP-binding state."""
        ...

    @property
    def live_manifest(self) -> dict[str, Any]:
        """The manifest currently live in this process."""
        ...


@runtime_checkable
class AppConfig(Protocol):
    """Access to the process config manager."""

    @property
    def config_manager(self) -> ConfigManager:
        """The process config manager."""
        ...


@runtime_checkable
class AppBackup(Protocol):
    """Registry for named backup sections and the run of one section's export/import.

    A plugin (or the host itself, the first consumer) registers a section under
    ``name`` by supplying an ``exporter()`` that returns a JSON-safe payload and
    an ``importer(payload)`` that applies it and returns a section report.
    ``sections()`` lists the registered sections for the UI. ``export_section``
    / ``import_section`` run one section's exporter/importer by name and raise
    loudly on an unknown name — never a silent no-op.
    """

    def register_section(
        self, name: str, exporter: Callable[[], Any], importer: Callable[[Any], Any], *, secret: bool = False
    ) -> None:
        """Register a backup section's exporter/importer under ``name``."""
        ...

    def sections(self) -> list[BackupSectionInfo]:
        """List the registered backup sections."""
        ...

    def export_section(self, name: str) -> Any:
        """Run one section's exporter by ``name`` and return its payload."""
        ...

    def import_section(self, name: str, payload: Any) -> Any:
        """Apply ``payload`` via one section's importer by ``name`` and return its report."""
        ...


@runtime_checkable
class AppSubApp(Protocol):
    """Access to the sub-app MCP router."""

    @property
    def mcp_sub_app_router(self) -> SubMcpAppRouter:
        """The router for mounted sub-app MCP servers."""
        ...
