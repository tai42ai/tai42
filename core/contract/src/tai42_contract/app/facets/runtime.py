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
    def register_monitoring(self, builder: Callable[..., Any] | None = None) -> Callable[..., Any]: ...

    @property
    def active(self) -> Monitoring:
        """The active monitoring backend (impl type behind the contract protocol)."""
        ...


@runtime_checkable
class AppClients(Protocol):
    def client_ctx[ClientT](
        self,
        client_cls: type[BaseClient[ClientT]],
        settings: Any = None,
        *,
        fresh: bool = False,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[ClientT]:
        """Async context manager yielding a connected client, pooled per loop +
        connection params (or one-shot when ``fresh=True``). ``settings`` is a kit
        ``ClientSettings`` whose ``client_kwargs()`` supplies the connection key."""
        ...

    async def shutdown_clients(self) -> None:
        """Close every live client pool for the running loop."""
        ...


@runtime_checkable
class AppLifecycle(Protocol):
    def on_startup(self, func: Callable[[], Any]) -> Callable[[], Any]: ...

    def on_shutdown(self, func: Callable[[], Any]) -> Callable[[], Any]: ...

    def on_reload(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register a handler re-run after every in-place re-init (``reload_config``) —
        e.g. dynamic tool loaders that ``on_startup`` ran once."""
        ...

    def on_post_swap(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register an establisher for a loop-affine background loop (a periodic poll or
        sweep). Run on the real serving loop at boot and after every epoch swap — never
        on the throwaway build-thread loop the per-epoch handlers run on — so the loop
        it spawns attaches to the serving loop and retires with its generation."""
        ...

    def on_fleet_op_applied(self, func: Callable[[str], Any]) -> Callable[[str], Any]:
        """Register a handler fired after any worker-bus op applies in this
        process, and after the reconnect self-resync reload.

        Unlike its zero-arg siblings, the handler takes ONE argument — the op
        name — so it can act on some ops and skip others (a query op carries no
        state change to react to)."""
        ...

    async def wait_until_ready(self) -> None:
        """Block until this process's first boot self-resync has completed — the
        point at which the tool registry is fully built and stable for the run.

        A backend runtime that forks a child (or otherwise consumes queued work)
        per job awaits this before its work loop accepts anything: the boot
        self-resync rebuilds the tool registry non-atomically, so a worker that
        dequeued and forked mid-rebuild would run the job against a half-built
        registry. The latch is one-way — it resolves once and stays resolved, so
        a later bus reconnect (which re-runs the self-resync while the app is
        already live) never un-readies the process."""
        ...


@runtime_checkable
class AppAdmin(Protocol):
    def reload_mcp(self, title: str) -> dict[str, Any]: ...

    def deregister_mcp(self, title: str) -> dict[str, Any]: ...

    def reload_config(self) -> dict[str, Any]: ...

    def tool_reloader(self, kind: str) -> Callable[..., Any]:
        """Register an async ``(action, name) -> dict`` reloader for ``kind``."""
        ...

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]: ...

    def reload_failed_mcps(self) -> list[dict[str, Any]]: ...

    def list_failed_mcps(self) -> list[dict[str, Any]]: ...

    def live_mcp_status(self) -> dict[str, Any]:
        """Snapshot the in-process MCP-binding state."""
        ...

    @property
    def live_manifest(self) -> dict[str, Any]: ...


@runtime_checkable
class AppConfig(Protocol):
    @property
    def config_manager(self) -> ConfigManager: ...


@runtime_checkable
class AppBackup(Protocol):
    """Registry for named backup sections and the run of one section's
    export/import.

    A plugin (or the host itself, the first consumer) registers a section under
    ``name`` by supplying an ``exporter()`` that returns a JSON-safe payload and
    an ``importer(payload)`` that applies it and returns a section report.
    ``sections()`` lists the registered sections for the UI. ``export_section``
    / ``import_section`` run one section's exporter/importer by name and raise
    loudly on an unknown name — never a silent no-op.
    """

    def register_section(
        self, name: str, exporter: Callable[[], Any], importer: Callable[[Any], Any], *, secret: bool = False
    ) -> None: ...

    def sections(self) -> list[BackupSectionInfo]: ...

    def export_section(self, name: str) -> Any: ...

    def import_section(self, name: str, payload: Any) -> Any: ...


@runtime_checkable
class AppSubApp(Protocol):
    @property
    def mcp_sub_app_router(self) -> SubMcpAppRouter: ...
