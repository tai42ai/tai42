"""The lifecycle mixin's construction-time state and the concrete-subclass spine it relies on."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.app.bus import WorkerBus
from tai42_skeleton.app.mount_map import MountBinding

if TYPE_CHECKING:
    import mcp
    from fastmcp import FastMCP
    from tai42_contract.config.manager import ConfigManager

    from tai42_skeleton.agent.binding import AgentBinding
    from tai42_skeleton.app.bus import WorkerKind
    from tai42_skeleton.app.clients import ClientsFacet
    from tai42_skeleton.app.http import HttpSurface
    from tai42_skeleton.app.server import ServingCore
    from tai42_skeleton.app.sessions import SessionRegistry
    from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
    from tai42_skeleton.backend.registry import BackendHolder
    from tai42_skeleton.backup import BackupRegistry
    from tai42_skeleton.channels.registry import ChannelRegistry
    from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
    from tai42_skeleton.extensions import ExtensionRegistry
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.presets.base_tool_config import PresetInputSchemaSupportRegistry
    from tai42_skeleton.presets.manager import PresetManager
    from tai42_skeleton.presets.seeds import PresetSeedRegistry
    from tai42_skeleton.presets.write_validators import PresetWriteValidatorRegistry
    from tai42_skeleton.sandbox import SandboxHolder
    from tai42_skeleton.states.seeds import StateTemplateSeedRegistry
    from tai42_skeleton.states.service import (
        StatesAttachReconcilerRegistry,
        StatesAttachValidatorRegistry,
        StatesConsumerListerRegistry,
        StatesService,
    )
    from tai42_skeleton.template import ResourceManager
    from tai42_skeleton.tools import ToolRefsRegistry, ToolRegistry, ToolRetryRegistry, ToolTierRegistry
    from tai42_skeleton.tools.binding import ToolBinding
    from tai42_skeleton.tools.delete_referees import ToolDeleteRefereeRegistry
    from tai42_skeleton.tools.detach_referees import StateTemplateDetachRefereeRegistry
    from tai42_skeleton.tools.rename_referees import ToolRenameRefereeRegistry
    from tai42_skeleton.webhooks.registry import WebhookVerifierRegistry


class LifecycleState(ABC):
    """Base holding the mutable per-epoch lifecycle state the app mixins read and swap."""

    def __init__(self):
        """Initialize the empty handler maps and per-epoch state slots."""
        # ``_manifest`` / ``_tool_registry`` / ``_extension_registry`` and the
        # MCP-binding maps below live on the per-epoch ``ServingCore`` and are reached
        # through the forwarding properties further down, so a build populates the epoch
        # under construction and a failed build is discarded with it.

        # Keyed by qualified name so a module re-import (each start() re-imports
        # the lifecycle modules) replaces rather than accumulates its handler,
        # while a construction-time (build_app) handler — registered once and
        # never re-imported — persists across reloads.
        self._startup_handlers: dict[str, Callable] = {}
        self._shutdown_handlers: dict[str, Callable] = {}

        # Dynamic tool loaders re-run after every re-init (update() drops all
        # tools). Keyed by qualified name so a module re-import replaces rather
        # than accumulates.
        self._reload_handlers: dict[str, Callable] = {}

        # Establishers for the loop-affine background loops that must run on the REAL
        # serving loop and retire with their generation (the advisories poll, the
        # conversations delivery sweep). Run at boot inside app_context (on the serving
        # loop) and AFTER every epoch swap by the build+swap primitive (also on the
        # serving loop) — never by the per-epoch handler list, which runs on a
        # throwaway build-thread loop a spawned task would not survive. Keyed by
        # qualified name so a module re-import replaces rather than accumulates.
        self._post_swap_handlers: dict[str, Callable] = {}

        # Per-kind tool reloaders, one per plugin-defined tool kind, registered
        # by the host app via @tool_reloader; fleet reload_tool/remove_tool
        # ops dispatch through run_tool_reload on every worker.
        self._tool_reloaders: dict[str, Callable] = {}

        # The one app-owned worker-bus subscription (cross-worker fleet ops). The
        # bus is internal infrastructure that SURVIVES reloads (it is not a manifest
        # plugin), so this process joins once in app_context and keeps a single
        # long-lived subscription for its whole lifetime — a reload re-imports the
        # backend but never tears down and rejoins the subscription.
        self._bus: WorkerBus | None = None
        self._bus_subscription_task: asyncio.Task[None] | None = None
        # Latched once this process's first boot self-resync completes: the tool
        # registry is then fully built and stable for the run. A forking/consuming
        # backend runtime awaits this (``wait_until_ready``) before its work loop
        # accepts a job, so a work-horse forked at boot never inherits a half-built
        # registry. One-way for the process lifetime — a reconnect self-resync
        # re-runs while the app is already live and must not un-ready it — so it is
        # NOT reset by ``start()``/reload.
        self._boot_ready = asyncio.Event()
        # The serving loop, remembered so a reload running on a worker thread can
        # marshal loop-affine work (preset reconcile, checkpoint/store close) back
        # onto it.
        self._serving_loop: asyncio.AbstractEventLoop | None = None

        # Handlers fired with the OP NAME after every applied bus op AND after the
        # reconnect self-resync reload. Keyed by qualified name so a module re-import
        # replaces rather than accumulates (mirrors the startup/reload registries). A
        # backend whose worker model needs post-op work registers here (celery's
        # prefork pool turnover is the known consumer).
        self._fleet_op_applied_handlers: dict[str, Callable] = {}

        # Lifespan-owned exponential-backoff task that re-probes failed-at-boot
        # MCP servers so a deploy-order race (the MCP pod comes up after the
        # skeleton) self-heals without a manual reload. Distinct from the
        # worker-bus subscription task above.
        self._reprobe_task: asyncio.Task[None] | None = None

        # The async-park expiry reaper loop, owned by app_context: an async ask
        # has no blocking waiter, so this loop is what fires a parked question's
        # continuation once its expiry passes. Runs until cancelled at shutdown.
        self._interactions_reaper_task: asyncio.Task[None] | None = None

        # The sandbox session reap loop, owned by app_context and spawned ONLY when a
        # sandbox provider is registered (a no-op absent one). Reaps expired sessions on
        # a fixed cadence; runs until cancelled at shutdown.
        self._sandbox_reaper_task: asyncio.Task[None] | None = None

        # The module → mount-binding map for the CURRENT registration pass, rebuilt at
        # the top of ``_initialize_components`` before any manifest module imports.
        self._mount_map: dict[str, MountBinding] = {}

    @abstractmethod
    def _mcp_tools(self, config: TaiMCPConfig, tools):
        raise NotImplementedError

    if TYPE_CHECKING:
        # Signatures of the methods invoked across concern-mixin boundaries, declared
        # so each mixin's ``self.<method>()`` reference resolves; the concrete
        # implementations live on the owning mixins the composite class inherits.
        async def _run_handlers(self, handlers: list[Callable], raise_on_error: bool = False): ...
        async def _run_post_swap_handlers(self, *, raise_on_error: bool) -> None: ...
        async def _run_fleet_op_applied_handlers(self, op_name: str) -> None: ...
        def _epoch_handlers(self) -> list[Callable]: ...
        def _refresh_manifest_mcp(self) -> None: ...
        def _require_live_manifest(self) -> "Manifest": ...
        @classmethod
        def _on_perpetual_task_done(cls, task: asyncio.Task[Any]) -> None: ...
        def _build_bus(self, kind: "WorkerKind") -> WorkerBus: ...
        def _spawn_bus_subscription(self) -> None: ...
        async def _cancel_bus_subscription(self) -> None: ...
        def start(self, manifest: "Manifest"):
            """Boot the app from ``manifest`` (implemented on the boot mixin)."""
            ...

        def _initialize_components(self): ...
        def _effective_router_modules(self) -> list[str]: ...
        def _build_mount_map(self) -> dict[str, MountBinding]: ...
        async def _probe_mcp(self, config: TaiMCPConfig, timeout: float | None = None) -> list["mcp.types.Tool"]: ...
        async def _load_mcps(
            self,
        ) -> tuple[list[tuple[TaiMCPConfig, Any]], list[tuple[TaiMCPConfig, str]]]: ...
        def _record_failed_mcp(self, config: TaiMCPConfig, kind: str) -> None: ...
        def _missing_tools_ignore(self) -> frozenset[str]: ...
        async def _apply_reloaded_mcp(self, title: str, config: TaiMCPConfig, tools: list[Any]) -> dict[str, Any]: ...
        def _spawn_reprobe_task(self) -> None: ...
        async def _cancel_reprobe_task(self) -> None: ...

    # Process-spine members the concrete subclass (``TaiMCP``) supplies; declared here
    # so the concern mixins' methods can reference them with a known type.
    _building: "ServingCore | None"
    _build_serving_core: Callable[[], "ServingCore"]
    clients: "ClientsFacet"
    preset_manager: "PresetManager"
    _config_manager: "ConfigManager"
    _mcp_sub_app_router: "SubMcpAppRouter"

    # Per-epoch collaborators, read/write through the forwarding properties
    # ``ServingCoreAccessMixin`` implements against the live serving core; their types
    # are declared here so every concern mixin's ``self._attr`` reference resolves.
    _manifest: "Manifest | None"
    _tool_registry: "ToolRegistry"
    _extension_registry: "ExtensionRegistry"
    _failed_mcps: dict[str, str]
    _mcp_bound_tools: dict[str, set[str]]
    _mcp_preset_conflicts: dict[str, set[str]]
    _resource_manager_cache: "ResourceManager | None"
    _fast_mcp: "FastMCP"
    _session_registry: "SessionRegistry"
    _tool_binding: "ToolBinding"
    _agent_binding: "AgentBinding"
    _backend_holder: "BackendHolder"
    _sandbox_holder: "SandboxHolder"
    _http_surface: "HttpSurface"
    _webhook_verifier_registry: "WebhookVerifierRegistry"
    _channel_registry: "ChannelRegistry"
    _write_validator_registry: "PresetWriteValidatorRegistry"
    _target_validator_registry: "TargetBindValidatorRegistry"
    _input_schema_support_registry: "PresetInputSchemaSupportRegistry"
    _registration_tier_registry: "ToolTierRegistry"
    _tool_refs_registry: "ToolRefsRegistry"
    _tool_retry_registry: "ToolRetryRegistry"
    _rename_referee_registry: "ToolRenameRefereeRegistry"
    _delete_referee_registry: "ToolDeleteRefereeRegistry"
    _detach_referee_registry: "StateTemplateDetachRefereeRegistry"
    _seed_registry: "PresetSeedRegistry"
    _backup_registry: "BackupRegistry"
    _states_service: "StatesService"
    _states_attach_validators: "StatesAttachValidatorRegistry"
    _states_attach_reconcilers: "StatesAttachReconcilerRegistry"
    _states_consumer_listers: "StatesConsumerListerRegistry"
    _states_template_seeds: "StateTemplateSeedRegistry"
    _preset_manager: "PresetManager"
