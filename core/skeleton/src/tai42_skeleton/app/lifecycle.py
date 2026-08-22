import asyncio
import inspect
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import mcp
from fastmcp import FastMCP
from fastmcp.prompts import Prompt
from fastmcp.resources import Resource
from fastmcp.resources.template import ResourceTemplate
from fastmcp.tools import Tool
from tai42_contract.access_control.registry import reset_registry as reset_identity_registry
from tai42_contract.accounts import reset_registry as reset_accounts_registry
from tai42_contract.app import tai42_app
from tai42_contract.manifest import TaiMCPConfig
from tai42_kit.clients import shutdown_all_clients
from tai42_kit.clients.impl.mcp import FastMCPClient
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.app.boot_rules import require_bus_for_backend, require_bus_for_k8s
from tai42_skeleton.app.bus import WorkerBus, WorkerKind
from tai42_skeleton.app.bus_settings import bus_settings
from tai42_skeleton.app.epoch import current_epoch, current_epoch_or_none, is_epoch_rebuild_in_progress
from tai42_skeleton.app.graceful_exit import graceful_exit_for
from tai42_skeleton.app.importer import import_or_reload_package
from tai42_skeleton.app.kind_status import collect_kind_status, warn_if_noop_monitoring
from tai42_skeleton.app.mount_map import MountBinding, bind_module, build_mount_map
from tai42_skeleton.app.readiness_sentinel import remove_ready_sentinel, write_ready_sentinel
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.route_defaults import DEFAULT_API_ROUTERS, STUDIO_SPA_ROUTER
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.connectors.providers.registry import reset_registry
from tai42_skeleton.connectors.token_injection import evict_pooled_session
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.extensions import ExtensionRegistry
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.middleware.rate_limit import warn_if_rate_limiting_off
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.settings.cache import mcp_probe_timeout, mcp_reload_probe_timeout
from tai42_skeleton.settings.settings import CoreSettings
from tai42_skeleton.tools import ToolRegistry, mcp_health
from tai42_skeleton.tools.adapters.mcp_tool_to_func import _detect_transport

if TYPE_CHECKING:
    from tai42_contract.config.manager import ConfigManager

    from tai42_skeleton.agent.binding import AgentBinding
    from tai42_skeleton.app.clients import ClientsFacet
    from tai42_skeleton.app.http import HttpSurface
    from tai42_skeleton.app.server import ServingCore
    from tai42_skeleton.app.sessions import SessionRegistry
    from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
    from tai42_skeleton.backend.registry import BackendHolder
    from tai42_skeleton.backup import BackupRegistry
    from tai42_skeleton.channels.registry import ChannelRegistry
    from tai42_skeleton.presets.base_tool_config import (
        PresetInputSchemaSupportRegistry,
        PresetRegistrationTierRegistry,
    )
    from tai42_skeleton.presets.manager import PresetManager
    from tai42_skeleton.presets.write_validators import PresetWriteValidatorRegistry
    from tai42_skeleton.sandbox import SandboxHolder
    from tai42_skeleton.template import ResourceManager
    from tai42_skeleton.tools import ToolRefsRegistry
    from tai42_skeleton.tools.binding import ToolBinding
    from tai42_skeleton.webhooks.registry import WebhookVerifierRegistry

logger = logging.getLogger(__name__)


def _op_field(op: dict[str, Any], key: str) -> Any:
    """A required field of a delivered fleet op, raising loudly when absent —
    a malformed fleet op must fail its confirmation, never apply a partial op."""
    value = op.get(key)
    if value is None:
        raise ValueError(f"{op.get('op')} fleet op missing {key!r}")
    return value


class TaiMCPLifecycleMixin(ABC):
    def __init__(self):
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

        # The async-park expiry reaper loop, owned by app_context: an async ask_user
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

    # -- per-epoch serving core (forwarding reads) -----------------------------
    # Every collaborator the lifecycle swaps per epoch is read through the live
    # serving generation's ``ServingCore``: the half-built core during a build (so
    # ``start()`` and the epoch handlers register into the epoch being built), else the
    # installed epoch's core. A reload that swaps in a fresh epoch is therefore visible
    # at every read site with no rebinding.

    @property
    def _serving_core(self) -> "ServingCore":
        if self._building is not None:
            return self._building
        core = current_epoch().core
        if core is None:
            raise RuntimeError("the live epoch has no serving core installed")
        return core

    @property
    def _live_serving_core(self) -> "ServingCore":
        """The LIVE epoch's core once one is installed; the pre-boot core before that.

        Request-path reads of per-epoch provider state resolve THIS, not the
        ``_building``-first :attr:`_serving_core`. During a reload a live epoch is ALWAYS
        installed (``current_epoch()`` is the outgoing generation until the atomic swap),
        so a request that interleaves with an in-flight build's serving-app enter sees the
        LIVE core and NEVER the generation under construction — a build that then FAILS can
        never leave a surviving epoch's memoized verifier bound onto the discarded
        generation's provider instances (the zero-mutation invariant). Before the boot
        epoch is installed there is no live generation, so this falls back to the sole
        pre-boot core (``_building``), which registration and the pre-boot harness read;
        no request is served in that window, so the being-built generation is never
        exposed here."""
        epoch = current_epoch_or_none()
        if epoch is not None and epoch.core is not None:
            return epoch.core
        if self._building is not None:
            return self._building
        raise RuntimeError("the live epoch has no serving core installed")

    # Generation state on the epoch's core, reached read/write through these forwarding
    # properties: ``start()`` and the runtime MCP mutators write the epoch under
    # construction during a build (``_building``) and the live epoch's core otherwise, so
    # a failed build never touches the serving generation. ``_manifest is None`` on a
    # fresh core is the pre-boot no-op contract the registration decorators check.

    @property
    def _manifest(self) -> "Manifest | None":
        return self._serving_core._manifest

    @_manifest.setter
    def _manifest(self, value: "Manifest | None") -> None:
        self._serving_core._manifest = value

    @property
    def _tool_registry(self) -> ToolRegistry:
        return self._serving_core._tool_registry

    @_tool_registry.setter
    def _tool_registry(self, value: ToolRegistry) -> None:
        self._serving_core._tool_registry = value

    @property
    def _extension_registry(self) -> ExtensionRegistry:
        return self._serving_core._extension_registry

    @_extension_registry.setter
    def _extension_registry(self, value: ExtensionRegistry) -> None:
        self._serving_core._extension_registry = value

    @property
    def _failed_mcps(self) -> dict[str, str]:
        return self._serving_core._failed_mcps

    @_failed_mcps.setter
    def _failed_mcps(self, value: dict[str, str]) -> None:
        self._serving_core._failed_mcps = value

    @property
    def _mcp_bound_tools(self) -> dict[str, set[str]]:
        return self._serving_core._mcp_bound_tools

    @_mcp_bound_tools.setter
    def _mcp_bound_tools(self, value: dict[str, set[str]]) -> None:
        self._serving_core._mcp_bound_tools = value

    @property
    def _mcp_preset_conflicts(self) -> dict[str, set[str]]:
        return self._serving_core._mcp_preset_conflicts

    @_mcp_preset_conflicts.setter
    def _mcp_preset_conflicts(self, value: dict[str, set[str]]) -> None:
        self._serving_core._mcp_preset_conflicts = value

    @property
    def _resource_manager_cache(self) -> "ResourceManager | None":
        return self._serving_core._resource_manager_cache

    @_resource_manager_cache.setter
    def _resource_manager_cache(self, value: "ResourceManager | None") -> None:
        self._serving_core._resource_manager_cache = value

    @property
    def _fast_mcp(self) -> FastMCP:
        return self._serving_core._fast_mcp

    @property
    def _session_registry(self) -> "SessionRegistry":
        return self._serving_core._session_registry

    @property
    def _tool_binding(self) -> "ToolBinding":
        return self._serving_core._tool_binding

    @property
    def _agent_binding(self) -> "AgentBinding":
        return self._serving_core._agent_binding

    @property
    def _backend_holder(self) -> "BackendHolder":
        return self._serving_core._backend_holder

    @property
    def _sandbox_holder(self) -> "SandboxHolder":
        return self._serving_core._sandbox_holder

    @property
    def _http_surface(self) -> "HttpSurface":
        return self._serving_core._http_surface

    @property
    def _webhook_verifier_registry(self) -> "WebhookVerifierRegistry":
        return self._serving_core._webhook_verifier_registry

    @property
    def _channel_registry(self) -> "ChannelRegistry":
        return self._serving_core._channel_registry

    @property
    def _write_validator_registry(self) -> "PresetWriteValidatorRegistry":
        return self._serving_core._write_validator_registry

    @property
    def _input_schema_support_registry(self) -> "PresetInputSchemaSupportRegistry":
        return self._serving_core._input_schema_support_registry

    @property
    def _registration_tier_registry(self) -> "PresetRegistrationTierRegistry":
        return self._serving_core._registration_tier_registry

    @property
    def _tool_refs_registry(self) -> "ToolRefsRegistry":
        return self._serving_core._tool_refs_registry

    @property
    def _backup_registry(self) -> "BackupRegistry":
        return self._serving_core._backup_registry

    @property
    def _preset_manager(self) -> "PresetManager":
        return self._serving_core._preset_manager

    def _on_startup(self, func: Callable):
        self._startup_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _on_reload(self, func: Callable):
        """Register a handler to re-run after every in-place re-init."""
        self._reload_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _on_post_swap(self, func: Callable):
        """Register a handler that establishes a loop-affine background loop on the
        serving loop — run at boot and after every epoch swap, never on the throwaway
        build-thread loop the per-epoch handlers run on."""
        self._post_swap_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _on_fleet_op_applied(self, func: Callable):
        """Register a handler fired with the OP NAME after every applied bus op and
        after the reconnect self-resync reload. Keyed by qualified name so a module
        re-import replaces rather than accumulates."""
        self._fleet_op_applied_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def _tool_reloader(self, kind: str) -> Callable:
        """Register an ``(action, name) -> dict`` reloader for one tool kind.
        Last registration wins (module re-imports re-run the decorator)."""

        def decorator(func: Callable) -> Callable:
            self._tool_reloaders[kind] = func
            return func

        return decorator

    async def _run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        """Apply one tool reload/remove via the registered reloader. Raises on
        an unknown kind/action or a failing reloader."""
        if action not in ("reload", "remove"):
            raise ValueError(f"Unknown tool-reload action {action!r} (expected 'reload' or 'remove')")
        reloader = self._tool_reloaders.get(kind)
        if reloader is None:
            raise RuntimeError(
                f"No tool reloader registered for kind {kind!r} (registered: {sorted(self._tool_reloaders)})"
            )
        if inspect.iscoroutinefunction(reloader):
            result = await reloader(action, name)
        else:
            result = reloader(action, name)
        return result or {"kind": kind, "action": action, "name": name, "status": "ok"}

    def _on_shutdown(self, func: Callable):
        self._shutdown_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    async def _run_handlers(self, handlers: list[Callable], raise_on_error: bool = False):
        """Run lifecycle handlers, always attempting every handler. The shutdown
        path swallows-and-logs so teardown reaches every handler; the startup and
        reload paths pass ``raise_on_error`` so a failed handler surfaces loudly
        instead of leaving a healthy-looking half-initialized app or reporting a
        successful reload with missing tools."""
        errors: list[tuple] = []
        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()
            except Exception as e:
                logger.error(f"Error in lifecycle handler {handler.__name__}: {e}", exc_info=True)
                errors.append((handler.__name__, e))
        if raise_on_error and errors:
            raise RuntimeError("lifecycle handlers failed: " + ", ".join(f"{name}: {exc!r}" for name, exc in errors))

    async def _run_post_swap_handlers(self, *, raise_on_error: bool) -> None:
        """Run the post-swap background-loop establishers on the CURRENT serving loop.

        The caller guarantees this runs on the real serving loop with the target
        generation already installed as ``current_epoch()``: at boot inside
        ``app_context``, and after every epoch swap inside the build+swap primitive. Each
        establisher (re)starts its loop-affine loop here and registers it with the live
        generation, so the loop attaches to the serving loop and retires with its epoch.
        Boot raises on failure (a broken boot must not look healthy); a post-swap reload
        does not — the fresh epoch already serves, so a background-loop start fault is
        loud but never unwinds the completed swap."""
        await self._run_handlers(list(self._post_swap_handlers.values()), raise_on_error=raise_on_error)

    def _read_boot_manifest(self) -> Manifest:
        """The one cold-boot manifest read every serving/backend entrypoint crosses.

        Bridges the persisted env store into ``os.environ`` BEFORE the manifest is
        resolved, so a manifest ``!ENV ${VAR}`` whose var lives only in the store
        resolves to its real value at boot rather than the silent ``"N/A"`` sentinel;
        reads + validates the manifest under the bridged env; then names any marker
        that STILL dangles. The ASGI serve worker, the stdio server, and the backend
        worker all boot through here, so the bridge holds on every serving door with
        no per-door copy — the same store the in-process reload path reads."""
        self._apply_stored_env_at_boot()
        manifest = Manifest.model_validate(self._config_manager.read_manifest())
        self._warn_dangling_boot_env_markers()
        return manifest

    def _apply_stored_env_at_boot(self) -> None:
        """Apply the persisted env store into ``os.environ`` at boot through the SAME
        apply-then-reset primitive an in-process reload uses, so boot and reload
        resolve settings under the store identically: stored env OVERRIDES container
        env, and any setting cached before this call is re-resolved under the bridged
        env. An absent store (no ``.env`` / no Secret — the managers' shared
        ``FileNotFoundError``) is an empty store, not a fault: zero keys applied. Only
        a genuinely unreadable store raises."""
        from tai42_skeleton.app.epoch import apply_env_and_reset_settings

        try:
            stored = self._config_manager.read_env()
        except FileNotFoundError:
            stored = {}
        apply_env_and_reset_settings(stored)
        logger.info("boot env bridge: applied %d stored-env key(s) into the process environment", len(stored))

    def _warn_dangling_boot_env_markers(self) -> None:
        """After the boot env bridge, name any manifest ``!ENV ${VAR}`` marker whose
        var is in NEITHER the container env nor the store — it silently resolved to the
        ``"N/A"`` sentinel. One loud WARNING listing the dangling var NAMES (never the
        marker values); boot proceeds, since the env-write boundary is the hard gate
        against INTRODUCING a dangling marker. Reads the PRESERVED manifest so the
        marker expressions survive unresolved and their var names can be named."""
        try:
            preserved = self._config_manager.read_manifest_preserved()
        except FileNotFoundError:
            return
        dangling = sorted(
            {ref.var for ref in scan_env_marker_refs(preserved) if ref.required and ref.var not in os.environ}
        )
        if dangling:
            logger.warning(
                'boot env bridge: %d manifest !ENV marker(s) resolve to the "N/A" sentinel — their env '
                "var is set in neither the container env nor the stored env: %s",
                len(dangling),
                ", ".join(dangling),
            )

    @asynccontextmanager
    async def app_context(self, manifest: Manifest, *, kind: WorkerKind = WorkerKind.serve):
        # Bus/boot invariant at the one seam both `tai serve` and `tai backend`
        # cross: a process with a registered backend, or a k8s-mode deployment, must
        # have the worker bus configured — otherwise sibling workers or sibling pods
        # serve stale config after a reload. Refuse loudly before the app starts.
        require_bus_for_k8s()
        require_bus_for_backend(manifest)
        from tai42_skeleton.app.epoch import clear_epoch, current_epoch_or_none, install_boot_core

        try:
            # Install boot epoch 0's serving core BEFORE start(), so start() and the
            # epoch handlers register into this generation's fresh FastMCP and every
            # process-spine read resolves to it. Built FRESH here (via the builder)
            # under the started env rather than reusing the construction-time scaffold,
            # so a sequential app_context in the same process never inherits a prior
            # generation's collaborators. Clearing ``_building`` routes reads through the
            # installed epoch; the worker lifespan attaches the dispatch slot + serving
            # app (an embedded / pure-``app_context`` caller needs none).
            self._building = self._build_serving_core()
            install_boot_core(self._building)
            self._building = None
            self.start(manifest)
            # raise_on_error: a failed startup handler must abort the boot
            # loudly, never leave a healthy-looking half-initialized app.
            await self._run_handlers(list(self._startup_handlers.values()), raise_on_error=True)
            # Bind the reload gate + remember the serving loop, so a reload running
            # on a worker thread can marshal loop-affine work (preset reconcile,
            # checkpoint/store close) back onto the serving loop.
            self._serving_loop = asyncio.get_running_loop()
            reload_gate.bind_to_running_loop()
            # Establish the boot generation's loop-affine background loops ON this
            # serving loop, registered with the boot epoch so they retire with it. Boot
            # raises loudly on a failed establisher (never a healthy-looking half-start).
            # A reload re-establishes them post-swap through the build+swap primitive.
            await self._run_post_swap_handlers(raise_on_error=True)
            # Join the worker bus: construct this process's one bus (worker kind
            # ``serve`` or ``backend``) and open its single long-lived subscription.
            # The subscription registers presence and self-resyncs (reload_config)
            # through on_ready on every (re)connect.
            self._bus = self._build_bus(kind)
            self._spawn_bus_subscription()
            # Start the failed-MCP re-probe task so a server that failed its boot
            # probe self-heals on an exponential backoff without a manual reload.
            self._spawn_reprobe_task()
            # Start the async-park expiry reaper so a parked ask_user's continuation
            # fires once its expiry passes even with no blocking waiter to trip it.
            self._spawn_interactions_reaper()
            # Start the sandbox session reap loop, but only when a provider backs the
            # slot — the reaper is a no-op door otherwise, so it is never spawned
            # without a provider to reap through.
            self._spawn_sandbox_reaper()
            yield self
        finally:
            # Shutdown start: drop the readiness sentinel FIRST so the readiness probe
            # fails immediately and this process leaves the load balancer's endpoints
            # before draining begins.
            remove_ready_sentinel()
            await self._cancel_bus_subscription()
            await self._cancel_reprobe_task()
            await self._cancel_interactions_reaper()
            await self._cancel_sandbox_reaper()
            # Shutdown keeps swallow-and-log so teardown runs every handler.
            await self._run_handlers(list(self._shutdown_handlers.values()))
            await self._teardown_resources()
            # Keep a serving-core read-target for any post-context read, then drop the
            # process serving generation (closing its FastMCP lifespan) so a later
            # app_context in this process starts from a clean slate.
            live = current_epoch_or_none()
            if live is not None and live.core is not None:
                self._building = live.core
            await clear_epoch()

    # --- worker-bus subscription (cross-worker fleet ops) ---

    @property
    def bus(self) -> WorkerBus:
        """This process's worker bus, built in ``app_context``. The runtime-op
        publishers and the fleet census route reach the fleet through it. Raises if
        accessed before ``app_context`` builds it."""
        bus = self._bus
        if bus is None:
            raise RuntimeError("the worker bus is not built — enter app_context first")
        return bus

    def _build_bus(self, kind: WorkerKind) -> WorkerBus:
        """Construct this process's one worker bus. With ``TAI_BUS_REDIS_URL`` set the
        real bus joins the fleet (its slot name + generation minted on the first claim
        at subscribe time); otherwise the no-op ``WorkerBus.local`` variant — legal
        only under the boot rules that permit a busless deployment (single worker, file
        mode, no backend)."""
        settings = bus_settings()
        if settings.enabled:
            return WorkerBus(settings, kind=kind)
        return WorkerBus.local(kind)

    def _spawn_bus_subscription(self) -> None:
        """Start the one long-lived bus subscription on the serving loop. Owned by
        ``app_context``; runs until cancelled at shutdown. The subscription reconnects
        with backoff internally and fires ``on_ready`` (the self-resync) after
        subscribe+presence-register on every (re)connect."""
        bus = self._bus
        if bus is None:
            raise RuntimeError("bus subscription spawned before the bus was built")
        self._bus_subscription_task = asyncio.create_task(
            bus.subscribe(self._apply_bus_op, on_ready=self._resync_on_ready),
            name="tai-worker-bus-subscription",
        )
        self._bus_subscription_task.add_done_callback(self._on_perpetual_task_done)

    async def _resync_on_ready(self) -> None:
        """Self-resync run after subscribe, before presence-register, on every
        (re)connect: a local ``reload_config`` re-reads persisted state so a broadcast
        missed while this worker was away self-heals. Routed through the same apply
        path as a delivered op so ``on_fleet_op_applied`` handlers fire — else a
        reconnecting celery worker would resync only its main process while its prefork
        children stay stale on the exact path the resync heals.

        A failing resync is non-fatal to the subscription. This runs inside
        ``subscribe`` BEFORE the message loop, so a propagating error would kill the
        subscription task with no reconnect — silently ejecting this worker from the
        fleet for the process lifetime. Instead the failure is ERROR-logged and
        swallowed so the subscription stays live (future broadcasts still reach this
        worker) and the next reconnect re-attempts the resync — an explicit, logged
        recovery. Cancellation propagates untouched.

        A SUCCESSFUL resync latches boot-ready (:meth:`_mark_boot_ready`): the tool
        registry is rebuilt and stable, so a backend runtime awaiting
        ``wait_until_ready`` may now consume work. A failed resync does NOT latch — a
        consumer stays blocked (and fails loudly on its own timeout) rather than
        forking against a registry a broken reload left half-built."""
        try:
            if self._boot_ready.is_set():
                # A RECONNECT: a reload_config broadcast may have been missed while
                # this worker was away, so re-read persisted config by building and
                # swapping in a fresh epoch. On the FIRST connect (boot) the live
                # config was JUST built by ``start()``, so a swap would be pure
                # redundant work (and would retire the just-built epoch) — skip it and
                # only latch boot-ready below.
                await self._apply_bus_op({"op": "reload_config"})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "worker bus: self-resync reload on reconnect failed — subscription stays live, "
                "resync retried on the next reconnect",
                exc_info=True,
            )
        else:
            self._mark_boot_ready()

    def _mark_boot_ready(self) -> None:
        """Latch the boot-ready signal on the FIRST successful self-resync. One-way:
        a reconnect resync re-enters here on a live app but must never clear the
        latch, so a consumer already past it is never retroactively un-readied.

        Writes the readiness sentinel BEFORE latching, so ``boot-ready`` and the
        sentinel the readiness probe tests are consistent: an unwritable sentinel path
        raises here (a loud boot fault), leaving the latch unset for the next reconnect
        to retry rather than reporting ready without the probe's signal."""
        if not self._boot_ready.is_set():
            write_ready_sentinel()
            logger.info("app boot-ready: first self-resync complete — tool registry built and stable")
            self._boot_ready.set()

    async def _wait_until_ready(self) -> None:
        """Backs ``app.lifecycle.wait_until_ready``: block until the first boot
        self-resync has latched boot-ready."""
        await self._boot_ready.wait()

    async def _cancel_bus_subscription(self) -> None:
        """Cancel the bus subscription and await its termination — the shutdown
        counterpart of ``_spawn_bus_subscription``.

        A task that died with a non-``CancelledError`` exception was already surfaced
        at ERROR by its done-callback, so it is awaited-and-swallowed here rather than
        re-raised — this runs inside ``app_context``'s shutdown ``finally``, and one
        dead task must not skip the remaining teardown."""
        task = self._bus_subscription_task
        self._bus_subscription_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Already logged at ERROR by the done-callback; swallowed so a dead
            # subscription cannot abort the remaining shutdown steps.
            pass

    async def _apply_bus_op(self, op: dict[str, Any]) -> Any:
        """Apply one fleet op delivered from a sibling worker (or the self-resync),
        then fire the post-apply hooks.

        The sibling-worker counterpart of a route-received admin call: it maps each op
        to the local admin primitive. The heavy sync ops run on a worker thread through
        the reload gate, identically to the HTTP path; the tool reload/remove ops
        dispatch through the async per-kind reloader; ``list_failed_mcps`` is a plain
        in-process read. An unrecognized op raises loudly.

        After the op applies, ``on_fleet_op_applied`` handlers fire with the op name —
        the op has not fully "applied" until they finish (a celery worker must re-fork
        its prefork pool before it reports applied), and a raising handler fails the op.
        The returned value becomes the op's terminal ``applied`` payload; the publisher
        echo-skips its own broadcast, so this never re-applies a self-op."""
        result = await self._dispatch_bus_op(op)
        await self._run_fleet_op_applied_handlers(_op_field(op, "op"))
        return result

    async def _dispatch_bus_op(self, op: dict[str, Any]) -> Any:
        """Map one fleet op to its local admin primitive and return its result."""
        op_name = op.get("op")
        if op_name == "reload_config":
            return await reload_gate.run(tai42_app.admin.reload_config, reimports=True)
        if op_name == "reload_mcp":
            title = _op_field(op, "title")
            return await reload_gate.run(lambda: tai42_app.admin.reload_mcp(title), reimports=False)
        if op_name == "deregister_mcp":
            title = _op_field(op, "title")
            return await reload_gate.run(lambda: tai42_app.admin.deregister_mcp(title), reimports=False)
        if op_name in ("reload_tool", "remove_tool"):
            action = "reload" if op_name == "reload_tool" else "remove"
            return await tai42_app.admin.run_tool_reload(_op_field(op, "kind"), action, _op_field(op, "name"))
        if op_name == "reload_failed_mcps":
            # Return the BARE list — the same shape the publisher's own self-apply
            # (operations/manifest.py) rides on its self entry — so every origin's
            # payload in one FleetResult is uniform (a consumer never special-cases
            # self vs remote for this op).
            return await reload_gate.run(tai42_app.admin.reload_failed_mcps, reimports=False)
        if op_name == "list_failed_mcps":
            # Bare list, matching the self-apply shape — see reload_failed_mcps above.
            return tai42_app.admin.list_failed_mcps()
        if op_name == "evict_template":
            # The template store was written on the origin worker; drop this worker's
            # stale compilation so its next render reflects the new content. ``prefix``
            # marks a directory delete (evict everything under the key).
            resource_manager = tai42_app.storage.resource_manager
            path = _op_field(op, "path")
            if op.get("prefix"):
                resource_manager.evict_dir(path)
            else:
                resource_manager.evict_compiled(path)
            return {"evicted": path}
        if op_name == "clear_template_cache":
            tai42_app.storage.resource_manager.clear_cache()
            return {"cleared": True}
        if op_name == "recycle":
            return await self._apply_recycle()
        raise ValueError(f"unknown fleet op {op_name!r}")

    async def _apply_recycle(self) -> dict[str, Any]:
        """Apply a targeted recycle op: write ``state=recycling`` into presence, arm
        the bus's single-shot post-terminal-reply slot with this process's graceful
        self-exit, then return the applied payload.

        The recycling state is written BEFORE arming the self-SIGTERM — the only viable
        seam, since the graceful-exit callable is sync and a ``create_task`` there would
        race the SIGTERM — so the census shows WHY this worker is departing. The exit
        fires only AFTER the terminal ``applied`` reply ships, so the orchestrator
        records a successful recycle before this process departs. The payload names the
        graceful-exit kind only — never an env value."""
        kind = self.bus.identity.kind
        await self.bus.mark_recycling()
        self.bus.arm_post_reply(graceful_exit_for(kind))
        return {"recycling": kind.value}

    async def _run_fleet_op_applied_handlers(self, op_name: str) -> None:
        """Fire every ``on_fleet_op_applied`` handler with the op name. A raising
        handler propagates (fail-loud), turning the op's terminal reply into
        ``failed`` — the post-apply obligation is part of the op applying."""
        for handler in list(self._fleet_op_applied_handlers.values()):
            if inspect.iscoroutinefunction(handler):
                await handler(op_name)
            else:
                handler(op_name)

    @staticmethod
    def _log_task_exception(task: asyncio.Task[Any]) -> bool:
        """Log a lifespan-owned background task's terminal exception at ERROR.

        Cancellation (shutdown) is the normal stop and stays silent;
        any other exception means the task stopped doing its job, so log it at
        ERROR with the task's name. Returns True when the task instead returned a
        value cleanly (no cancellation, no exception), letting a caller treat an
        unexpected clean return as its own failure."""
        if task.cancelled():
            return False
        exc = task.exception()
        if exc is not None:
            logger.error("background task %r terminated with an exception", task.get_name(), exc_info=exc)
            return False
        return True

    @classmethod
    def _on_perpetual_task_done(cls, task: asyncio.Task[Any]) -> None:
        """Done-callback for a run-until-cancelled lifespan-owned task (the
        worker-bus subscription and the failed-MCP re-probe loop).

        A clean cancellation stays silent and a runtime exception is logged at
        ERROR — and, unlike a bounded task, a NORMAL return is ALSO logged at
        ERROR: these tasks are contractually perpetual, so returning means the
        worker silently stopped doing its job (a subscription that returns stops
        receiving sibling reloads; the re-probe loop stops self-healing)."""
        if cls._log_task_exception(task):
            logger.error(
                "perpetual background task %r returned unexpectedly; it must run until cancelled",
                task.get_name(),
            )

    async def _teardown_resources(self) -> None:
        """Release process-wide resources at shutdown.

        Runs on ``app_context`` — the single seam both the served (HTTP / stdio)
        and backend-worker entrypoints cross — so neither path leaks pooled
        clients or the langgraph store/checkpoint pools, nor loses buffered
        monitoring spans. Each step runs independently so one failure cannot skip
        the rest; collected failures are re-raised together so they surface
        loudly rather than being swallowed.
        """
        errors: list[Exception] = []

        async def _guard(label: str, teardown: Callable[[], Any]) -> None:
            try:
                await teardown()
            except Exception as e:
                logger.error("Error during %s teardown: %s", label, e, exc_info=True)
                errors.append(e)

        await _guard("pooled clients", self.clients.shutdown_clients)
        await _guard("checkpoint registry", lambda: checkpoint_registry().close_all())
        await _guard("store registry", lambda: store_registry().close_all())

        # Flush buffered monitoring spans last, so spans emitted during the
        # teardown above are captured before the process exits rather than lost
        # to a SIGTERM / container restart ahead of the SDK's periodic flush.
        try:
            get_monitoring().writer.flush()
        except Exception as e:
            logger.error("Error during monitoring flush: %s", e, exc_info=True)
            errors.append(e)

        if errors:
            raise ExceptionGroup("shutdown teardown failed", errors)

    def start(self, manifest: Manifest):
        # Booting is the composition root: this is where the running impl claims
        # the global ``tai42_app`` handle. Constructing a ``TaiMCP`` must not — only
        # start()/app_context binds, so building a throwaway app can't hijack it.
        tai42_app.bind(self)
        self._manifest = manifest
        self._failed_mcps = {}
        self._mcp_bound_tools = {}
        # Reset so a dropped agent doesn't linger across update()/reload —
        # importer.py re-fires the @tai42_app.agents.agent decorator each start().
        self._agent_binding.reset()

        # Reset so a dropped webhook verifier doesn't linger across update()/reload
        # — the manifest's verifier modules re-run their register() call each
        # start(). Mirrors the agent reset above.
        self._webhook_verifier_registry.reset()

        # Reset so a dropped channel doesn't linger across update()/reload — the
        # manifest's channel modules re-run their register() call each start().
        # Mirrors the webhook-verifier reset above.
        self._channel_registry.reset()

        # Reset so a dropped preset write validator doesn't linger across
        # update()/reload — the manifest's tool modules re-run their
        # register_write_validator() call each start(). Mirrors the resets above.
        self._write_validator_registry.reset()

        # Reset the per-base-tool preset input-schema support + registration-tier
        # declarations alongside the write validator, for the same reload reason.
        self._input_schema_support_registry.reset()
        self._registration_tier_registry.reset()

        # Reset so a dropped tool-references declaration doesn't linger across
        # update()/reload — the manifest's tool modules re-run their
        # @app.tools.tool(tool_refs=...) decorator each start(). Mirrors the reset above.
        self._tool_refs_registry.reset()

        # Drop the cached resource manager: a reload re-imports the storage
        # module and rebuilds the storage provider, so a stale cache would keep
        # rendering against (and pin open) the previous provider's pool.
        self._resource_manager_cache = None

        # Clear the connector registry's write-target generation before
        # _initialize_components() re-registers the manifest's ``connectors`` entries
        # during boot/reload; the registry's duplicate guard would otherwise crash on
        # the re-registration. During an epoch build this clears the fresh STAGED
        # generation (the live catalog stays untouched); at boot the committed map.
        # Mirrors the _agents reset.
        reset_registry()

        # Clear the identity-provider registry's write-target generation before
        # _initialize_components() re-imports the manifest's identity-plugin modules,
        # which re-run their module-level register_identity_provider(...) calls.
        # The skeleton ships NO concrete identity provider: a deployment names one
        # (e.g. tai42_identity_redis.redis_api_key_provider, the default in the example
        # manifest) in its manifest lifecycle_modules, which _initialize_components
        # imports below — that import-only registration is the sole home, exactly as
        # the shared_secret webhook verifier registers.
        reset_identity_registry()

        # Clear the accounts-provider registry beside the identity reset: an accounts
        # provider registers into BOTH registries on import, so without this mirror an
        # in-process reload would clear identity but not accounts, and the duplicate
        # guard in register_accounts_provider would crash the reload once an accounts
        # plugin is installed.
        reset_accounts_registry()

        # Drop sub-MCP routes + cached sub-apps so a re-init stops serving
        # sub-apps from the previous generation; the reload handlers below
        # re-register the current ones. Their lifespans tear down on the loop
        # that owns them.
        self._mcp_sub_app_router.reset()

        # Clear the live tool/prompt/resource/template surface before
        # _initialize_components() re-imports the manifest modules and re-fires their
        # module-level @tai42_app.tools.tool (and any module-registered prompt /
        # resource / resource-template) decorators, so each re-registration lands in
        # a clean surface and never trips on_duplicate="error". Mirrors the
        # agent/webhook/channel/identity resets above: tools are the same
        # shape (a module decorator re-fired every reload), so they get the same
        # reset-before-reimport treatment rather than relying on the CALLER's removal
        # being atomic with the reimport — an interleaving reload whose caller-side
        # removal had not fully cleared the surface would otherwise re-add a
        # still-present tool and crash the reload with "Component already exists".
        self._reset_component_surface()

        # Clear the operation registry's write-target generation before
        # _initialize_components() re-imports the router modules, which re-fire their
        # module-level @operation decorators; without this a reload would trip the
        # duplicate-name guard. During an epoch build this clears the fresh STAGED
        # generation and the replay/route-attach/projection below all read it, so the
        # live committed surface keeps answering authz/dispatch against a complete
        # generation the whole time — no unsettled window on the request path. At
        # boot it clears the committed map.
        operation_registry.clear()

        # Clear the route registry's write-target /api shape generation before the
        # routers re-attach: an epoch build clears the fresh STAGED generation (the
        # live match/collision surface stays untouched); at boot the committed one.
        # The re-import below repopulates it, so an uninstalled/remapped route leaves
        # the shape index by simply not re-registering.
        route_registry.reset_shape_index()

        self._initialize_registries()
        self._initialize_components()

        # The route index must be dropped HERE, AFTER the reimport re-attached every
        # route: a request served in the reload window can have rebuilt it against the
        # previous surface. A stale index resolves an added route to None, which denies
        # every caller at the tool edge and skips the route's fence at the request gate.
        from tai42_skeleton.access_control.role_gate import reset_route_index

        reset_route_index()

        logger.info("[tools]")
        for t in sorted(self._registry_names_sync()["tool"]):
            logger.info(f"\t. {t}")

        # The pluggable-kind summary: one line per kind's live active/default/off
        # state (a broken registry raises here and fails the boot, never a silent
        # partial table), plus the once-per-process warning when NoOp monitoring is
        # the active recorder — the point where "not configured" becomes
        # distinguishable from "no traffic".
        kind_rows = collect_kind_status()
        logger.info("[kinds]")
        for row in kind_rows:
            suffix = f" ({row.plugin})" if row.plugin else ""
            logger.info(f"\t. {row.kind}: {row.state}{suffix} — {row.detail}")
        warn_if_noop_monitoring(kind_rows, logger)
        # The public doors run unthrottled when no Redis backs the rate limiter —
        # one loud once-per-process WARNING here (never a boot refusal).
        warn_if_rate_limiting_off(logger)

    def _epoch_handlers(self) -> list[Callable]:
        """The ONE ordered per-epoch handler list: the startup handlers then the
        reload handlers, de-duplicated by qualified name so a handler registered on
        BOTH hooks (the presets/sub-MCP/studio rehydration set) runs exactly once per
        epoch build. First-occurrence order is preserved, so the startup ordering —
        including presets-before-sub-MCP — holds; the reload-only handlers append
        after. Every startup handler runs per epoch (eager provider init included), so
        a rebuilt epoch's providers are instantiated before its first request."""
        return list({**self._startup_handlers, **self._reload_handlers}.values())

    def _reload_registries(self, manifest: Manifest) -> dict[str, Any]:
        """Re-initialise the process registries from ``manifest`` and run the per-epoch
        handler list ONCE — the rebuild step the epoch build+swap primitive calls.

        The proposed env is already live in ``os.environ`` and the settings caches
        were cleared by the primitive, so this resolves every settings read under the
        env about to be persisted. It raises loudly on ANY failure so the
        primitive discards the half-built epoch and restores the env — there is no
        restore-on-failure dance here (the primitive owns the discard)."""
        # Reload-time re-check of the backend-needs-bus invariant, BEFORE the
        # registries rebuild: a reload whose new manifest registers a backend while
        # the bus is unconfigured is refused here (an env-materialized backend or an
        # out-of-band manifest edit). The k8s rule is boot-fixed env, no reload twin.
        require_bus_for_backend(manifest)
        self.start(manifest)
        self._run_blocking(lambda: self._run_handlers(self._epoch_handlers(), raise_on_error=True))
        # Audit the STAGED route generation before the build commits it: fail the rebuild
        # loudly if a plugin still declared in the manifest lost every one of its routes on
        # the re-import, so the atomic swap never installs a route-dropping epoch (the old
        # epoch keeps serving on the raise).
        self._audit_plugin_routes_preserved()
        return {"status": "ok"}

    def _audit_plugin_routes_preserved(self) -> None:
        """Assert the epoch rebuild kept the routes of every still-declared plugin — the
        loud guard against a silent route unmount. ``expected_owners`` is the plugin owner
        identity of each route-declaring mount binding this build resolved (a plugin
        dropped from the manifest, or bound route-less, is absent, so its legitimately-gone
        routes never trip the guard). Uses the ONE owner-identity source ``custom_route``
        stamps rows under, so the two never drift."""
        from tai42_skeleton.app.http import plugin_owner

        expected_owners = {
            plugin_owner(binding) for binding in (self._mount_map or {}).values() if binding.declared_routes
        }
        route_registry.audit_plugin_routes_preserved(expected_owners)

    def _initialize_registries(self):
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        self._tool_registry = ToolRegistry(
            requested_tools=self._manifest.tools_list,
            tool_extensions=self._manifest.tool_extensions,
        )
        self._extension_registry = ExtensionRegistry(self._tool_registry.used_extensions)

    def _effective_router_modules(self) -> list[str]:
        """The router modules to import this boot, composed from the default set +
        the manifest's own list per ``default_routers``.

        - ``"all"``: the default API routers, then the manifest's extras, then the
          Studio SPA catch-all forced LAST.
        - ``"api"``: the default API routers, then extras, and NO SPA catch-all —
          unless the operator explicitly listed it (then it is honored last).
        - ``"none"``: no defaults; ``routers_modules`` is authoritative, with an
          operator-listed catch-all still moved to the end.

        Recomputed fresh on every start()/reload so the composition holds across
        reloads. The ``module not in effective`` check is the no-double-mount guard:
        a manifest that still lists a defaulted core router imports it exactly once,
        so the catch-all is never accidentally un-lasted and no route registers
        twice. The ``module != STUDIO_SPA_ROUTER`` check keeps the catch-all out of
        the middle even when a manifest lists it among the extras — it is only ever
        appended by the two branches below, always last.
        """
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")

        listed = self._manifest.routers_modules or []
        effective: list[str] = []
        if self._manifest.default_routers != "none":
            effective.extend(DEFAULT_API_ROUTERS)
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
        records = self._run_blocking(lambda: MarketplaceInstallStore().list_installed())
        return {record.ref: dict(record.route_mounts) for record in records}

    def _initialize_components(self):
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")

        # Put the configured plugin prefix on sys.path BEFORE any manifest module
        # imports below, so a plugin whose distribution lives in the persistent
        # prefix imports at boot after a restart. Appended at the END so the
        # environment shadows the prefix for anything present in both; a no-op when
        # no prefix is configured. Imported here (not at module scope) to keep the
        # app import chain free of the marketplace package.
        from tai42_skeleton.marketplace.compat import distribution_map
        from tai42_skeleton.marketplace.prefix import activate_prefix
        from tai42_skeleton.plugins.quarantine import reset_quarantine

        activate_prefix()

        # This pass owns the plugin-quarantine generation: reset here, then every
        # additive-module import below (and the Studio-plugin registry rebuild
        # handler that runs after start()) repopulates it. One package→dist
        # snapshot serves every compat verdict of the pass.
        reset_quarantine()
        dist_map = distribution_map()

        # Repopulate the operation registry that start() cleared at its top. A leaf
        # module declares its operations with @operation at IMPORT, and that
        # decorator fires exactly once per interpreter; a plain re-import of a router
        # that only ``from operations.<domain> import <op>`` never re-registers a
        # leaf that stayed cached in sys.modules, so without this the projection
        # below would find an empty registry and project nothing. The first call
        # re-imports the leaves to fire each @operation into the registry (never the
        # operations package or its infra, so the registry singleton the projection
        # and authz hold is preserved) and snapshots the records; a reload replays that
        # stable in-memory snapshot instead of re-importing, so the reload adds no
        # sys.modules churn on the loop-affine path. Runs BEFORE the routers below so
        # each route re-attaches its template + method to the metadata record already IN
        # the registry (the record the projection reads and the tool-edge authorization
        # synthesizes its concrete path from).
        #
        # Imported here rather than at module scope: the app import chain pulls the
        # operations package in, so a module-level import of it would be circular.
        from tai42_skeleton.operations import reregister_operations

        reregister_operations()

        # Build the module → mount-binding map BEFORE any manifest module imports, so
        # each declared plugin route resolves its absolute path + public flag from the
        # declaration while its module imports below (bound through
        # ``_import_additive_plugin``). Reserved-prefix violations fail the build here.
        self._mount_map = self._build_mount_map()

        # Register the manifest's declarative connector providers before importing
        # any manifest module. A connector is pure data (no import path), so it is
        # registered here during boot/reload through the module-global app handle's
        # ``connectors`` facet — the same ``AppConnectors.register_connector`` seam
        # any holder of the handle uses. ``start()`` cleared the write-target
        # generation above, so every (re)load re-registers from the current manifest.
        # A duplicate id across entries is a boot/epoch-build failure: the registry's
        # duplicate guard raises here rather than quarantining and continuing.
        for descriptor in self._manifest.connectors:
            tai42_app.connectors.register_connector(descriptor)

        # The pass's run-once ledger: a package walk under one role can sweep in a
        # module that is ALSO its own manifest entry under another role (a root package
        # listed under a non-route role whose route submodules are their own router
        # entries). The ledger records every module body a role loop runs so a later
        # loop does not run it again — exactly one execution per module per pass. Local,
        # so a fresh epoch build starts with an empty ledger (never leaks across reloads).
        executed_modules: set[str] = set()

        for module in self._manifest.lifecycle_modules or []:
            self._import_additive_plugin(module, "lifecycle", dist_map, executed_modules)

        # Identity/accounts providers register only on lifecycle-module import, so
        # their registries are settled here — abort now if a configured provider
        # quarantined instead of quarantine-and-continuing into an unauthable boot.
        self._abort_if_auth_provider_quarantined()

        # Import-only verifier plugins: each import runs the module's
        # ``tai42_app.webhook_verifiers.register(...)`` side-effect — the registry
        # was reset at the top of start(), so every (re)load re-registers cleanly.
        for module in self._manifest.webhook_verifier_modules or []:
            self._import_additive_plugin(module, "webhook_verifier", dist_map, executed_modules)

        # Import-only channel plugins: each import runs the module's
        # ``tai42_app.channels.register(...)`` side-effect and binds the plugin's
        # inbound HTTP route. Imported like the verifier modules — the registry
        # was reset at the top of start(), so every (re)load re-registers cleanly.
        for module in self._manifest.channel_modules or []:
            self._import_additive_plugin(module, "channel", dist_map, executed_modules)

        # Import the composed effective router set (defaults + extras + a single
        # last catch-all, per default_routers). Each import runs the module's
        # @custom_route decorators, registering routes into the FastMCP route table.
        # The epoch build rebuilds the serving ASGI app from that table and the atomic
        # swap installs it, so a router first imported on a live reload serves the
        # instant the swap completes — no restart.
        for module in self._effective_router_modules():
            self._import_additive_plugin(module, "router", dist_map, executed_modules)

        for module in self._manifest.middlewares_modules or []:
            self._import_additive_plugin(module, "middleware", dist_map, executed_modules)

        # The scalar slots ABORT boot on incompat/import failure instead of
        # quarantining: the server cannot run without its backend/sandbox/storage/
        # monitoring, so a skipped slot would be a silently crippled server.
        self._import_core_plugin(self._manifest.backend_module, "backend_module", dist_map, executed_modules)
        self._import_core_plugin(self._manifest.sandbox_module, "sandbox_module", dist_map, executed_modules)
        self._import_core_plugin(self._manifest.storage_module, "storage_module", dist_map, executed_modules)
        self._import_core_plugin(self._manifest.monitoring_module, "monitoring_module", dist_map, executed_modules)

        quarantined_extension_modules: set[str] = set()
        for extension in self._manifest.extensions_modules or []:
            if not self._import_additive_plugin(extension, "extensions", dist_map, executed_modules):
                quarantined_extension_modules.add(extension)
        try:
            self._extension_registry.validation()
        except TaiValidationError:
            # A quarantined extensions module cannot say WHICH extension names it
            # would have registered, so its missing extensions are indistinguishable
            # from the quarantine's own footprint — attributed to it loudly here
            # instead of aborting the boot the quarantine just saved. With no
            # quarantined extensions module the failure is genuine manifest
            # misconfiguration and stays a loud abort.
            if not quarantined_extension_modules:
                raise
            logger.error(
                "extension validation failed with quarantined extensions module(s) %s; "
                "continuing — tools using their extensions fail at bind/call time",
                sorted(quarantined_extension_modules),
                exc_info=True,
            )

        quarantined_tool_modules: set[str] = set()
        for cfg in self._manifest.tools:
            if not self._import_additive_plugin(cfg.module, "tools", dist_map, executed_modules):
                quarantined_tool_modules.add(cfg.module)

        # Importing an agents-module fires its @tai42_app.agents.agent decorator, which
        # registers the agent + auto-generates its run tool. Done after tools so
        # an agent tool can reference a base tool already loaded above.
        for cfg in self._manifest.agents:
            self._import_additive_plugin(cfg.module, "agents", dist_map, executed_modules)

        if self._manifest.mcp:
            successes, failures = self._run_blocking(self._load_mcps)

            for cfg, tools in successes:
                self._mcp_tools(cfg, tools)

            for cfg, kind in failures:
                self._record_failed_mcp(cfg, kind)

        # Health history follows the manifest: a title dropped from config drops its
        # history; surviving titles keep continuity across reloads. Fires on every
        # epoch build against the CONFIGURED titles (a failed/unavailable title keeps
        # its history), so an empty manifest clears the store.
        mcp_health.retain({cfg.title for cfg in self._manifest.mcp or []})

        # Project the operation surface into MCP tools. Runs AFTER the router
        # modules registered their operations and AFTER base tools/MCPs bound (so
        # extension combos over a projected op resolve at bind time), and BEFORE
        # validation and the preset-rehydration reload handlers re-bake — the
        # pinned order registries/routes -> operations -> projection -> extension
        # wraps -> preset rebakes.
        project_operations(self, self._manifest.api_tools)

        # A quarantined tools module's included tool names are legitimately absent
        # (the module never imported), so they join the failed-MCP ignore set —
        # otherwise the validation would abort the very boot the quarantine saved.
        ignore = set(self._missing_tools_ignore())
        for module in quarantined_tool_modules:
            ignore |= self._manifest.include_module_tools_map.get(module, frozenset())
        self._tool_registry.validation(ignore=frozenset(ignore))

    def _import_additive_plugin(
        self, module: str, kind: str, dist_map: dict[str, list[str]], executed_modules: set[str]
    ) -> bool:
        """Import one ADDITIVE manifest module under the plugin-compat gate.

        An incompatible module is never imported (importing it is exactly what
        crash-loops or misbehaves), and ANY exception its import raises — not
        only ImportError; contract drift surfaces as AttributeError/TypeError
        just as readily — quarantines the module instead of aborting boot. Both
        paths record a quarantine entry (one loud log line each) and return
        ``False`` so the caller can account for the module's absent
        contributions; ``True`` means the module imported. An unknown verdict
        (no dist mapping / no declared range) proceeds with a logged note,
        never a silent pass. Imports are function-local to keep the app import
        chain free of the marketplace package.

        ``executed_modules`` is the pass's ledger of already-run module bodies: a
        module a prior role loop's package walk already executed under its own
        binding (the accounts-postgres shape — a root package under a non-route role
        whose route submodules are also their own router entries) is not re-imported
        here, so each module body runs EXACTLY once per pass. On success the modules
        this import ran are added to the ledger.
        """
        from tai42_skeleton.app.http import plugin_owner
        from tai42_skeleton.marketplace.compat import module_compat
        from tai42_skeleton.plugins.quarantine import quarantine_plugin

        if module in executed_modules:
            return True
        verdict = module_compat(module, dist_map)
        if verdict.status == "incompatible":
            quarantine_plugin(module, f"{kind} module not loaded: {verdict.reason}")
            return False
        if verdict.status == "unknown":
            logger.info("plugin compat unknown for %s module %s: %s", kind, module, verdict.reason)
        binding = self._mount_map.get(module)
        # Savepoint the FastMCP route table before a BOUND module imports, so a failure
        # can roll back exactly the routes it committed. A bindingless (core/operator)
        # module records no owner-isolable rows, so it takes no savepoint/rollback — but a
        # route submodule this walk sweeps in under ITS OWN binding is guarded per-module
        # by the importer through the savepoint/rollback handles passed below.
        savepoint = self._http_surface.route_table_savepoint() if binding is not None else None
        reloaded: list[str] = []
        try:
            # A mount-bound module resolves its declared routes through the binding
            # carried on the contextvar for the span of its import; the bind also
            # verifies every declared row registered once the import completes. A bound
            # plugin may register its routes in a SIBLING of the manifest leaf (the leaf
            # imports the sibling for the ``@custom_route`` side-effect); re-importing
            # the leaf alone leaves that sibling cached in ``sys.modules`` so its
            # decorators never re-fire and its routes drop from the rebuilt epoch. Pop
            # those sibling module(s) — the ones the live epoch recorded for THIS owner,
            # never a wider set — so they re-fire under this same binding on reload. A
            # self-registering leaf (its routes declared in the leaf itself) records only
            # itself, so its extra set is empty and no other module is disturbed. Only a
            # route-registering module is re-fired here, so a non-route import side-effect
            # must live in a manifest-listed module to run on reload, never in a
            # route-sibling that only the extras pop.
            extra = route_registry.owner_route_modules(plugin_owner(binding)) - {module} if binding is not None else ()
            with bind_module(binding):
                reloaded = import_or_reload_package(
                    module,
                    extra,
                    mount_map=self._mount_map,
                    route_savepoint=self._http_surface.route_table_savepoint,
                    route_rollback=self._http_surface.rollback_module_routes,
                )
        except Exception as exc:
            if binding is not None and savepoint is not None:
                # Roll back so a quarantined declared-route module serves NOTHING: the
                # rows it committed before a mid-import custom_route raise, or before a
                # post-import _verify_all_registered raise, must leave no trace in the
                # shape index, _routes, or the FastMCP route table.
                self._http_surface.rollback_module_routes(binding, savepoint)
            logger.exception("%s module %s failed to import; quarantining it", kind, module)
            quarantine_plugin(module, f"{kind} module failed to import: {exc}")
            return False
        executed_modules.update(reloaded)
        return True

    def _import_core_plugin(
        self, module: str | None, slot: str, dist_map: dict[str, list[str]], executed_modules: set[str]
    ) -> None:
        """Import one SCALAR-slot module (backend/storage/monitoring), aborting
        boot with the typed :class:`CorePluginBootError` on incompat or ANY
        import failure — the server cannot run without its scalar slots, so a
        quarantine-and-continue would be a silently crippled server. ``None``
        (slot unset) is a no-op. The error names the plugin, the versions in
        play (via the compat reason), and the remedy.

        ``executed_modules`` is the pass's run-once ledger: a slot module a prior
        role loop's walk already executed is not re-imported, and the modules this
        import runs join the ledger."""
        if not module:
            return
        if module in executed_modules:
            return
        from tai42_skeleton.marketplace.compat import CorePluginBootError, module_compat

        verdict = module_compat(module, dist_map)
        if verdict.status == "incompatible":
            raise CorePluginBootError(
                f"{slot} plugin {module!r} cannot boot: {verdict.reason}; the server cannot run without its {slot}"
            )
        if verdict.status == "unknown":
            logger.info("plugin compat unknown for %s %s: %s", slot, module, verdict.reason)
        try:
            reloaded = import_or_reload_package(
                module,
                mount_map=self._mount_map,
                route_savepoint=self._http_surface.route_table_savepoint,
                route_rollback=self._http_surface.rollback_module_routes,
            )
        except Exception as exc:
            raise CorePluginBootError(
                f"{slot} plugin {module!r} failed to import: {exc}; the server cannot run without its {slot} — "
                "fix or update the plugin, or point the manifest at a working one"
            ) from exc
        executed_modules.update(reloaded)

    def _abort_if_auth_provider_quarantined(self) -> None:
        """Abort boot when a configured auth provider quarantined — the auth-slot
        twin of the scalar-slot abort.

        Identity/accounts providers are ADDITIVE, so a broken one quarantines rather
        than aborting; but a quarantined auth provider leaves the server BOOTED yet
        unauthable, and the quarantine's own report (marketplace listing / Studio)
        sits behind the very auth that is gone — so the additive loud-failure premise
        collapses for the one kind whose failure hides its own report. An accounts
        provider registers into the identity registry too, so every configured
        provider resolves through ``auth_providers``.

        The gate fires when BOTH hold this boot: a configured provider did not
        register AND some lifecycle module quarantined. It CANNOT prove the
        quarantine caused the missing provider — an operator typo in a provider
        name plus an unrelated quarantine trips the same predicate — so the error
        enumerates the two facts SEPARATELY (the unresolved provider names; the
        quarantined lifecycle modules with reasons) with no causal claim, and a
        remedy covering both. With the gate off, no unresolved provider, or no
        lifecycle quarantine, boot is untouched; a misconfigured provider name
        with nothing quarantined is left to the identity-provider startup probe."""
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        from tai42_contract.access_control.registry import get_identity_provider_factory_staged

        from tai42_skeleton.access_control.settings import access_control_settings
        from tai42_skeleton.marketplace.compat import CorePluginBootError
        from tai42_skeleton.plugins.quarantine import quarantined_plugins_staged

        settings = access_control_settings()
        if not settings.enable:
            return

        def _registered(name: str) -> bool:
            # Read the STAGED generation: this build's own decision keys on what THIS
            # build imported, not the live epoch's registry.
            try:
                get_identity_provider_factory_staged(name)
                return True
            except KeyError:
                return False

        unresolved = [name for name in settings.auth_providers if not _registered(name)]
        lifecycle_modules = set(self._manifest.lifecycle_modules or [])
        quarantined = {
            module: reason for module, reason in quarantined_plugins_staged().items() if module in lifecycle_modules
        }
        if unresolved and quarantined:
            detail = "; ".join(f"{module} ({reason})" for module, reason in sorted(quarantined.items()))
            raise CorePluginBootError(
                f"access control is enabled but configured auth provider(s) {sorted(unresolved)} did not register, "
                f"and lifecycle module(s) quarantined this boot: {detail}; the server would boot unauthable with any "
                "quarantine surfaced only behind the missing auth — fix the provider name(s) if misspelled, and/or "
                "resolve the quarantined plugin(s), or point the manifest at a working provider"
            )

    def _registry_names_sync(self) -> dict[str, set[str]]:
        """Snapshot the live server's tool / prompt / resource names off-loop —
        used by ``start()``'s tool log. Keyed by the SINGULAR kind. Runs through the
        one off-loop ``_run_blocking`` runner, safe from a loop-less caller and from
        inside the server loop."""

        async def snapshot() -> dict[str, set[str]]:
            return {
                "tool": {t.name for t in await self._fast_mcp.list_tools()},
                "prompt": {p.name for p in await self._fast_mcp.list_prompts()},
                "resource": {str(r.uri) for r in await self._fast_mcp.list_resources()},
            }

        return self._run_blocking(snapshot)

    def _reset_component_surface(self) -> None:
        """Clear every tool / prompt / resource / resource template off the live
        ``local_provider``.

        Called at the top of ``start()`` (before ``_initialize_components``
        re-imports the manifest modules) so a module-level ``@tai42_app.tools.tool``
        — or any module-registered prompt / resource / resource-template — decorator
        that re-fires on the reimport always adds into a clean surface, never
        tripping ``on_duplicate="error"``.

        Enumerates the provider's OWN stored components, deliberately NOT the server
        ``list_*`` views: those filter (enabled / visibility / auth) and synthesize
        (prefab renderer resources computed on demand, resident on no provider), so
        a filtered-out component would survive the reset and re-collide on the
        re-fire, and a synthetic resource URI has nothing to remove and would raise.
        The raw provider surface is exactly the set the re-fired decorators
        (re-)populate. ``ResourceTemplate`` is a distinct component kind (not a
        ``Resource`` subclass) under its own key namespace, so it is cleared on its
        own branch. Names/URIs are de-duplicated so a versioned/unversioned mix
        cannot double-remove one name (``remove_*`` clears all versions by name in
        one call). Synchronous — a plain dict read and sync ``remove_*`` calls,
        needing no event loop, so it runs inline wherever ``start()`` runs (the
        serving loop at cold boot, a worker thread on reload)."""
        provider = self._fast_mcp.local_provider
        components = list(provider._components.values())
        for name in {c.name for c in components if isinstance(c, Tool)}:
            provider.remove_tool(name)
        for name in {c.name for c in components if isinstance(c, Prompt)}:
            provider.remove_prompt(name)
        # ResourceTemplate is a distinct component kind, NOT a Resource subclass, so
        # the Resource branch never sweeps it — it needs its own removal branch (the
        # four kinds are disjoint, so the branch order is immaterial).
        for uri_template in {c.uri_template for c in components if isinstance(c, ResourceTemplate)}:
            provider.remove_template(uri_template)
        for uri in {str(c.uri) for c in components if isinstance(c, Resource)}:
            provider.remove_resource(uri)

    async def _probe_mcp(self, config: TaiMCPConfig, timeout: float | None = None) -> list["mcp.types.Tool"]:
        """Connect to one MCP server and list its tools, bounded by ``timeout``
        (defaults to the cold-boot ``mcp_probe_timeout``). Raises on
        failure/timeout; callers decide whether to skip-and-record or surface the
        error. The probe runs through the pooled ``FastMCPClient`` (one-shot,
        off-pool) so no raw fastmcp ``Client`` is opened by the app."""

        async def _do() -> list["mcp.types.Tool"]:
            async with self.clients.client_ctx(FastMCPClient, fresh=True, config=config.model_dump()) as client:
                return await client.list_tools()

        return await asyncio.wait_for(_do(), timeout=timeout if timeout is not None else mcp_probe_timeout())

    async def _load_mcps(
        self,
    ) -> tuple[list[tuple[TaiMCPConfig, Any]], list[tuple[TaiMCPConfig, str]]]:
        """Probe every manifest MCP server concurrently, each isolated.

        Returns ``(successes, failures)`` and never raises for one server, so a
        dead/slow MCP can't abort startup. Driven off-loop through
        ``_run_blocking`` so a sync (Celery/RQ) caller and the serving loop both
        reach it safely.
        """
        manifest = self._manifest
        if manifest is None or not manifest.mcp:
            return [], []

        # A RELOAD holds the reload gate while the fleet is live, so an unreachable server
        # must not block the probe for the generous cold-boot budget — it would stall every
        # reload-gated write and fleet-reload convergence. Use the short reload budget for a
        # rebuild (the re-probe task / ``reload_failed_mcps`` door binds a laggard a moment
        # later); keep the full budget only for the one-time cold boot.
        timeout = mcp_reload_probe_timeout() if is_epoch_rebuild_in_progress() else mcp_probe_timeout()

        async def run_one(config: TaiMCPConfig):
            try:
                tools = await self._probe_mcp(config, timeout=timeout)
                return config, tools, None
            except Exception as e:
                return config, None, type(e).__name__

        results = await asyncio.gather(*(run_one(cfg) for cfg in manifest.mcp))
        successes, failures = [], []
        for config, tools, kind in results:
            if kind is None:
                successes.append((config, tools))
            else:
                failures.append((config, kind))
        return successes, failures

    def _record_failed_mcp(self, config: TaiMCPConfig, kind: str) -> None:
        """Record a failed MCP as ``unavailable`` and log it.

        Stores only the title + coarse status, never the exception text or
        config — ``list_failed_mcps`` is LLM-callable and the config carries
        credentials. Only ``kind`` (exception class name) reaches the log.
        """
        self._failed_mcps[config.title] = "unavailable"
        logger.error(
            "MCP server '%s' unavailable — skipped, recorded for reload (%s)",
            config.title,
            kind,
        )

    def _missing_tools_ignore(self) -> frozenset[str]:
        """Tool names the failed MCP servers were to provide — legitimately
        absent (server down), so ``tools.validation`` must not raise for them.

        Matched by base name (``:``-extension stripped), so validation is
        slightly under-strict for a missing tool sharing a base name with a
        failed MCP's tool — collisions are unlikely and not crashing wins.
        """
        ignore: set[str] = set()
        title_map = getattr(self._manifest, "include_title_mcp_tools_map", {}) or {}
        for title in self._failed_mcps:
            ignore |= set(title_map.get(title, set()))
        return frozenset(ignore)

    # --- targeted single-MCP reload (boot recovery + agent tools) ---

    def _list_failed_mcps(self) -> list[dict[str, str]]:
        """MCP servers skipped due to a failed viability check: ``title`` +
        coarse ``status`` only. No config, no exception text — this is
        LLM-callable, logged and broadcast, and the config carries
        credentials. Per-process: in a multi-worker backend this reflects only
        the current process.

        Reads race a reload worker thread mutating ``_failed_mcps`` (this read is
        deliberately not reload-gated, so status keeps answering mid-reload), so
        the dict is snapshot-copied — a single C-level op, atomic under the GIL —
        before iterating."""
        return [{"title": title, "status": status} for title, status in dict(self._failed_mcps).items()]

    def _require_live_manifest(self) -> Manifest:
        """The live in-process manifest, or a loud error if the app is not started
        — the typed accessor behind ``app.admin.live_manifest_typed``."""
        manifest = self._manifest
        if manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        return manifest

    def _refresh_manifest_mcp(self) -> None:
        """Graft the manifest's current MCP rows into the boot-time snapshot so
        a row written after boot is reloadable / a removed one deregisterable.
        When no external manifest is readable (embedded/test runtimes) the
        in-memory copy stays authoritative."""
        try:
            fresh = Manifest.model_validate(self._config_manager.read_manifest())
        except FileNotFoundError:
            logger.warning(
                "no external manifest to re-read; using the in-memory MCP rows",
                exc_info=True,
            )
            return
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        self._manifest.replace_mcp(fresh.mcp)

    async def _reload_mcp_async(self, title: str) -> dict[str, Any]:
        self._refresh_manifest_mcp()
        manifest = self._manifest
        mcp_map = (manifest.mcp_map if manifest else {}) or {}
        if title not in mcp_map:
            return {
                "title": title,
                "status": "error",
                "error": f"Unknown MCP '{title}' — not present in the current manifest.",
            }

        config = mcp_map[title]
        try:
            tools = await self._probe_mcp(config)
        except Exception as e:
            self._record_failed_mcp(config, type(e).__name__)
            return {"title": title, "status": "unavailable"}

        return await self._apply_reloaded_mcp(title, config, tools)

    async def _apply_reloaded_mcp(self, title: str, config: TaiMCPConfig, tools: list[Any]) -> dict[str, Any]:
        """Bind a freshly-probed MCP server's tools and reconcile dependent presets —
        the registry-mutating half of a reload, split from the probe.

        This half synchronously rewrites the process-wide tool registry and then
        marshals the preset reconcile onto the serving loop, so two servers applying
        at once would race each other's registry mutation across the probe and serving
        threads. ``_reload_failed_mcps_async`` therefore probes servers concurrently
        but calls this ONE server at a time; the single-server reload calls it once.
        """
        # Heal the connection this reload manages: drop the pooled dispatch session
        # for this title so the next dispatch builds a fresh one. The probe passed on
        # a throwaway off-pool client, so a dead pooled session survives it and every
        # dispatch keeps hitting the corpse until it is evicted here.
        await self._evict_mcp_session_on_serving_loop(config)

        # Clean reload: drop any tools this MCP previously bound, then rebind.
        old_bound = set(self._mcp_bound_tools.get(title, set()))
        for name in sorted(old_bound):
            try:
                self._fast_mcp.local_provider.remove_tool(name)
            except Exception:
                logger.warning("reload_mcp: could not remove stale tool %s", name, exc_info=True)

        self._mcp_tools(config, tools)
        new_bound = set(self._mcp_bound_tools.get(title, set()))

        # Symmetry with _deregister_mcp: a tool this MCP no longer serves must ALSO
        # leave the base registry, or a re-probed server drops T from the wire while
        # T lingers in _requested_tools/_tools (its self-entry keeps it out of
        # missing_tools) — a registry-derived surface stays stale until a full
        # reload.
        for name in sorted(old_bound - new_bound):
            self._tool_registry.unregister_tool_base(name)

        await self._reconcile_after_mcp_reload(title, old_bound, new_bound)

        self._failed_mcps.pop(title, None)
        result: dict[str, Any] = {
            "title": title,
            "status": "ok",
            "tools": sorted(new_bound),
        }
        # A name the (re)bind refused because a registered preset owns it — surfaced
        # loudly so the caller sees the returning server did NOT clobber the preset.
        conflicts = sorted(self._mcp_preset_conflicts.get(title, set()))
        if conflicts:
            result["preset_conflicts"] = conflicts
        return result

    async def _reconcile_after_mcp_reload(self, title: str, old_bound: set[str], new_bound: set[str]) -> None:
        """Reconcile dependent presets after a targeted MCP reload rebinds
        ``title``'s tools and the vanished-tool unregistration has settled the base
        registry.

        A preset over a base this MCP rebound is a ``TransformedTool`` whose closure
        still holds the OLD wrapper/config, so it is re-registered from its in-memory
        spec to track the freshly-bound base; a preset whose base vanished across the
        reload is quarantined (its store row surfaces as ``conflicted``).
        ``old_bound`` / ``new_bound`` are the tool names this MCP bound before and
        after the rebind,
        so their union is exactly the set of bases whose bindings changed."""
        await self._reconcile_bases_on_serving_loop(old_bound | new_bound)

    async def _reconcile_bases_on_serving_loop(self, affected_bases: set[str]) -> None:
        """Reconcile base-dependent presets with the ``PresetManager`` per-name locks
        taken on the serving loop.

        Those locks are ``asyncio.Lock``s, valid on a single loop only. The preset
        mutation routes take them on the serving loop, so every reconcile takes them
        there too: a route mutating a preset and a reconcile touching the same name
        contend on ONE loop, never two (a cross-loop contended acquire raises, and a
        lock touched from two threads gives no mutual exclusion). The reprobe pass
        already runs this ON the serving loop and awaits directly; an admin
        reload/deregister runs its body on a ``_run_blocking`` worker loop and
        marshals the coroutine back onto the serving loop, awaiting the cross-loop
        result without blocking the worker loop. With no serving loop bound (a
        pure-sync boot) nothing else contends, so the reconcile runs on the current
        loop.
        """
        loop = self._serving_loop
        if loop is None or asyncio.get_running_loop() is loop:
            await self.preset_manager.reconcile_bases(affected_bases)
            return
        future = asyncio.run_coroutine_threadsafe(self.preset_manager.reconcile_bases(affected_bases), loop)
        await asyncio.wrap_future(future)

    async def _evict_mcp_session_on_serving_loop(self, config: TaiMCPConfig) -> None:
        """Evict the pooled dispatch session for ``config`` on the serving loop.

        The ``FastMCPClient`` pools are per event loop and dispatch runs on the
        serving loop, so an eviction on any other loop misses the real pool and
        leaves the dead session in place. An admin reload runs this body on a
        ``_run_blocking`` worker loop and marshals the eviction back onto the
        serving loop; the reprobe pass already runs on the serving loop and awaits
        directly, and a pure-sync boot (no serving loop bound) runs it on the
        current loop. The transport is detected once here and reused, never
        re-derived. A failed eviction propagates — it must not silently leave a
        corpse in the pool.
        """
        transport = _detect_transport(config.config)

        async def _evict() -> None:
            await evict_pooled_session(config, transport, FastMCPClient())

        loop = self._serving_loop
        if loop is None or asyncio.get_running_loop() is loop:
            await _evict()
            return
        future = asyncio.run_coroutine_threadsafe(_evict(), loop)
        await asyncio.wrap_future(future)

    async def _reload_failed_mcps_async(self) -> list[dict[str, Any]]:
        """Re-probe every currently-failed MCP concurrently, then apply the binds
        ONE server at a time.

        Probing is network-bound and mutates no shared state, so all servers are
        probed at once — N down servers cost ~one probe timeout, not N. Applying a
        probe result rewrites the process-wide tool/preset registry and marshals the
        reconcile onto the serving loop, so applications are serialized: running two
        at once would let one server's bind race another's reconcile across the probe
        and serving threads. Titles are snapshotted before any mutation.
        """
        self._refresh_manifest_mcp()
        manifest = self._manifest
        mcp_map = (manifest.mcp_map if manifest else {}) or {}
        titles = list(self._failed_mcps.keys())
        known = [t for t in titles if t in mcp_map]
        unknown = [t for t in titles if t not in mcp_map]

        probes = await asyncio.gather(*(self._probe_mcp(mcp_map[t]) for t in known), return_exceptions=True)

        out: list[dict[str, Any]] = []
        for title, probe in zip(known, probes, strict=True):
            config = mcp_map[title]
            if isinstance(probe, BaseException):
                self._record_failed_mcp(config, type(probe).__name__)
                out.append({"title": title, "status": "unavailable"})
                continue
            try:
                out.append(await self._apply_reloaded_mcp(title, config, probe))
            except Exception:
                # A post-probe bind/reconcile failure must not lose the other titles'
                # results — log the trace loudly, then surface this one coarsely.
                logger.error("reload_failed_mcps: applying reloaded MCP %r failed after probe", title, exc_info=True)
                out.append({"title": title, "status": "error"})
        for title in unknown:
            out.append(
                {
                    "title": title,
                    "status": "error",
                    "error": f"Unknown MCP '{title}' — not present in the current manifest.",
                }
            )
        return out

    @staticmethod
    def _run_blocking(coro_factory: Callable[[], Any]) -> Any:
        """Run a coroutine to completion regardless of the caller's context.

        The single off-loop runner: a private event loop on a worker thread, safe
        from a loop-less caller and from inside the server loop. Every off-loop
        snapshot / probe / reload-handler run goes through here.
        """

        async def _run_and_cleanup() -> Any:
            try:
                return await coro_factory()
            finally:
                # asyncio.run tears down this throwaway loop without closing the
                # per-loop pooled clients the coroutine opened (reload handlers
                # open pooled clients via app.clients.client_ctx), so close them
                # here before teardown. A cleanup failure is logged loudly but
                # must not replace the coroutine's result or its exception.
                try:
                    await shutdown_all_clients()
                except Exception:
                    logger.exception("Error closing pooled clients after _run_blocking")

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(_run_and_cleanup())).result()

    def _raise_if_on_serving_loop(self, op: str) -> None:
        """Refuse a reconcile-driving admin call issued from a coroutine already on
        the serving loop.

        ``reload_mcp`` / ``reload_failed_mcps`` / ``deregister_mcp`` run their async
        body through ``_run_blocking`` and marshal the preset reconcile back onto the
        serving loop. Called from the serving loop itself, ``_run_blocking`` would
        freeze that loop on its blocking wait, so the marshaled reconcile could never
        run — a silent deadlock. Raise loudly instead. The supported callers —
        ``reload_gate.run``'s worker thread and a loop-less sync caller — have no
        running loop here (or a different one) and pass through.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running is self._serving_loop:
            raise RuntimeError(
                f"{op} must not be called from the serving loop; drive it through "
                "reload_gate.run(...) or an off-loop/sync caller"
            )

    def _reload_mcp(self, title: str) -> dict[str, Any]:
        """Re-probe one MCP server by title and, if viable, (re)attach its tools.
        Synchronous and context-agnostic, mirroring ``update``.

        On failure the server is re-recorded in ``list_failed_mcps`` and its
        existing tools are left intact (transient blips self-heal). Only a
        successful reload replaces tools; a mid-rebind failure can leave the set
        partially updated — rerun after fixing the config.
        """
        self._raise_if_on_serving_loop("reload_mcp")
        return self._run_blocking(lambda: self._reload_mcp_async(title))

    def _reload_failed_mcps(self) -> list[dict[str, Any]]:
        """Re-probe every MCP currently in the failed list; attach the ones
        that are now viable. Synchronous and context-agnostic."""
        self._raise_if_on_serving_loop("reload_failed_mcps")
        return self._run_blocking(self._reload_failed_mcps_async)

    # --- lifespan-owned failed-MCP backoff re-probe ---

    def _spawn_reprobe_task(self) -> None:
        """Start the failed-MCP re-probe loop on the serving loop. Owned by
        ``app_context``; runs until cancelled at shutdown."""
        self._reprobe_task = asyncio.create_task(
            self._reprobe_failed_mcps_loop(),
            name="tai-failed-mcp-reprobe",
        )
        # Backstop a silent death OR an unexpected normal return loudly, mirroring
        # the worker-bus subscription: both are run-until-cancelled tasks.
        self._reprobe_task.add_done_callback(self._on_perpetual_task_done)

    async def _cancel_reprobe_task(self) -> None:
        """Cancel the re-probe task and await its termination — the shutdown
        counterpart of ``_spawn_reprobe_task``.

        A task that died with a non-``CancelledError`` exception was already
        surfaced at ERROR by its done-callback, so it is awaited-and-swallowed
        here rather than re-raised — this runs inside ``app_context``'s shutdown
        ``finally``, and re-raising would skip the remaining teardown."""
        task = self._reprobe_task
        self._reprobe_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Already logged at ERROR by the done-callback; swallowed so a dead
            # re-probe task cannot abort the remaining shutdown steps.
            pass

    async def _reprobe_sleep(self, seconds: float) -> None:
        """The re-probe loop's inter-pass sleep, isolated so tests can drive the
        backoff with a controllable clock instead of real time."""
        await asyncio.sleep(seconds)

    # --- lifespan-owned async-park expiry reaper ---

    def _spawn_interactions_reaper(self) -> None:
        """Start the async-park expiry reaper loop on the serving loop. Owned by
        ``app_context``; runs until cancelled at shutdown. A no-op each pass when the
        interactions store is unconfigured."""
        from tai42_skeleton.interactions.reaper import run_expiry_reaper_loop

        self._interactions_reaper_task = asyncio.create_task(
            run_expiry_reaper_loop(),
            name="tai-interactions-expiry-reaper",
        )
        # Backstop a silent death OR an unexpected normal return loudly, mirroring the
        # worker-bus subscription and re-probe loop: a run-until-cancelled task.
        self._interactions_reaper_task.add_done_callback(self._on_perpetual_task_done)

    async def _cancel_interactions_reaper(self) -> None:
        """Cancel the expiry reaper and await its termination — the shutdown
        counterpart of ``_spawn_interactions_reaper``.

        A non-``CancelledError`` death was already surfaced at ERROR by the
        done-callback, so it is awaited-and-swallowed here (this runs inside
        ``app_context``'s shutdown ``finally``, where re-raising would skip the
        remaining teardown)."""
        task = self._interactions_reaper_task
        self._interactions_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _spawn_sandbox_reaper(self) -> None:
        """Start the sandbox session reap loop on the serving loop, but ONLY when a
        provider is registered — the loop is never started absent one. Owned by
        ``app_context``; runs until cancelled at shutdown."""
        if self._sandbox_holder.sandbox is None:
            return
        from tai42_skeleton.sandbox import run_sandbox_reap_loop

        self._sandbox_reaper_task = asyncio.create_task(
            run_sandbox_reap_loop(),
            name="tai-sandbox-reaper",
        )
        # Backstop a silent death OR an unexpected normal return loudly, mirroring the
        # interactions expiry reaper: a run-until-cancelled task.
        self._sandbox_reaper_task.add_done_callback(self._on_perpetual_task_done)

    async def _cancel_sandbox_reaper(self) -> None:
        """Cancel the sandbox reap loop and await its termination — the shutdown
        counterpart of ``_spawn_sandbox_reaper``.

        A non-``CancelledError`` death was already surfaced at ERROR by the
        done-callback, so it is awaited-and-swallowed here (this runs inside
        ``app_context``'s shutdown ``finally``, where re-raising would skip the
        remaining teardown)."""
        task = self._sandbox_reaper_task
        self._sandbox_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _probe_and_apply_failed(self, snapshot: dict[str, TaiMCPConfig]) -> tuple[list[dict[str, Any]], set[str]]:
        """Re-probe the snapshotted failed MCP servers OFF the reload gate, then bind
        the recovered ones back UNDER it.

        ``snapshot`` is the ``{title: config}`` a reprobe pass captured under the
        gate. The probe is a network read that mutates no shared state, so it runs
        unlocked and on the SHORT reload budget (parity with ``_load_mcps``, not the
        generous cold-boot timeout) — a persistently-down server therefore never
        holds the gate across the probe. The gate is re-acquired only to apply, and
        each title is re-verified STILL failed AND STILL in the manifest before its
        bind: a concurrent admin reload/deregister may have cleared or removed it
        while probing, and a stale/removed title must not be re-applied. Returns the
        per-title results and the gate-consistent still-failed set.
        """
        titles = list(snapshot)
        probes = await asyncio.gather(
            *(self._probe_mcp(snapshot[t], timeout=mcp_reload_probe_timeout()) for t in titles),
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        # Every read/write of ``_failed_mcps`` (and the bind it drives) happens under
        # the reload gate; a worker-thread reload holds the same lock, so the
        # re-verify and apply below cannot race a concurrent mutation.
        async with reload_gate.lock:
            manifest = self._manifest
            mcp_map = (manifest.mcp_map if manifest else {}) or {}
            for title, probe in zip(titles, probes, strict=True):
                if title not in self._failed_mcps or title not in mcp_map:
                    # Cleared or removed by a concurrent admin reload/deregister while
                    # probing — a stale/removed title must not be re-applied.
                    continue
                config = snapshot[title]
                if isinstance(probe, BaseException):
                    self._record_failed_mcp(config, type(probe).__name__)
                    out.append({"title": title, "status": "unavailable"})
                    continue
                try:
                    out.append(await self._apply_reloaded_mcp(title, config, probe))
                except Exception:
                    # A post-probe bind/reconcile failure must not lose the other
                    # titles' results — log the trace loudly, surface this one coarsely.
                    logger.error(
                        "reload_failed_mcps: applying reloaded MCP %r failed after probe", title, exc_info=True
                    )
                    out.append({"title": title, "status": "error"})
            still_failed = set(self._failed_mcps)
        return out, still_failed

    async def _reprobe_failed_mcps_loop(self) -> None:
        """Re-probe failed-at-boot MCP servers on an exponential backoff.

        Each pass sleeps the current interval, then — only when a server is
        currently failed — SNAPSHOTS the failed titles under the reload gate, PROBES
        them off the gate on the short reload budget, and re-acquires the gate to
        rebind the recovered tools and clear them from the failed set, logging the
        outcome at INFO. The gate wraps only the brief snapshot and apply, never the
        probe: holding it across the network probe would stall every reload-gated
        write for up to the probe timeout each pass. The interval starts at
        ``mcp_reprobe_initial_seconds``, doubles (capped at
        ``mcp_reprobe_max_seconds``) after a pass where every probed server stayed
        down, and resets to the initial value the moment any server recovers or a
        new one appears in the failed set. An empty failed set probes nothing.

        A cancellation (shutdown) propagates for a clean exit; any other per-pass
        error is logged loudly and the loop survives to the next pass — a silently
        dead recovery task is the exact failure mode this task removes.
        """
        interval = CoreSettings().mcp_reprobe_initial_seconds
        # Titles known-failed as of the previous pass; a title present now but
        # absent here is a fresh failure and resets the backoff to probe promptly.
        # Seeded under the reload-gate lock on the first pass (below) so the
        # snapshot never races a worker-thread reload mutating the failed set.
        known_failed: set[str] | None = None
        while True:
            await self._reprobe_sleep(interval)
            try:
                settings = CoreSettings()
                initial = settings.mcp_reprobe_initial_seconds
                # SNAPSHOT the failed set + each title's probe config under the
                # reload-gate lock: a worker-thread reload holds the same lock, so
                # this is the only place the failed set is read concurrency-safely (a
                # bare snapshot would race a concurrent mutation → dict-changed-size).
                # The probe that follows runs OFF the gate.
                async with reload_gate.lock:
                    self._refresh_manifest_mcp()
                    manifest = self._manifest
                    mcp_map = (manifest.mcp_map if manifest else {}) or {}
                    current_failed = set(self._failed_mcps)
                    if known_failed is None:
                        known_failed = current_failed
                    if not current_failed:
                        interval = initial
                        known_failed = set()
                        continue
                    newly_appeared = not current_failed <= known_failed
                    snapshot = {t: mcp_map[t] for t in current_failed if t in mcp_map}
                # Probe off the gate, then re-acquire it to bind the recovered
                # servers and read the settled failed set (both gate-consistent).
                results, still_failed = await self._probe_and_apply_failed(snapshot)
                recovered = sorted(r["title"] for r in results if r.get("status") == "ok")
                logger.info(
                    "failed-MCP re-probe pass: probed=%s recovered=%s still_failed=%s",
                    sorted(current_failed),
                    recovered,
                    sorted(still_failed),
                )
                if recovered or newly_appeared:
                    interval = initial
                else:
                    interval = min(interval * 2, settings.mcp_reprobe_max_seconds)
                known_failed = still_failed
            except Exception:
                # ``CancelledError`` is a BaseException and passes this handler for
                # a clean shutdown cancel; every other error is logged and the loop
                # continues at the current backoff.
                logger.error("failed-MCP re-probe pass failed; retrying next interval", exc_info=True)

    def _deregister_mcp(self, title: str) -> dict[str, Any]:
        """Detach one MCP server's tools — the removal counterpart of
        ``reload_mcp``. Idempotent: a process that never bound the title
        reports ``absent``, not an error."""
        self._raise_if_on_serving_loop("deregister_mcp")
        self._refresh_manifest_mcp()
        bound = sorted(self._mcp_bound_tools.pop(title, set()))
        failed = self._failed_mcps.pop(title, None) is not None
        # The title ceases to exist here — clear its passive health so a removed
        # MCP leaves no residue behind in the process-wide store.
        mcp_health.forget(title)
        if not bound and not failed:
            return {"title": title, "status": "absent"}
        for name in bound:
            try:
                self._fast_mcp.local_provider.remove_tool(name)
            except Exception:
                logger.warning("deregister_mcp: could not remove tool %s", name, exc_info=True)
            self._tool_registry.unregister_tool_base(name)
        # Reconcile presets that depended on the just-removed bases: a dependent
        # preset is quarantined (its store row surfaces as ``conflicted``) — no
        # dependent preset is left bound to a base that no longer exists.
        # This method is synchronous (context-agnostic, mirroring ``update``), so the
        # async reconciliation runs through the off-loop blocking runner, which
        # marshals the ``PresetManager`` locks onto the serving loop.
        if bound:
            self._run_blocking(lambda: self._reconcile_bases_on_serving_loop(set(bound)))
        return {"title": title, "status": "ok", "removed": bound}

    def _reload_config(self) -> dict[str, Any]:
        """Soft restart: build a FRESH serving epoch under the persisted env and swap
        it in atomically — env refresh + settings reset + registry rebuild +
        fresh serving surface + retire of the previous generation, all in the one
        ``build_and_swap_epoch`` primitive. Heavy but in-process — no pod restart. A
        reload-added router serves after the swap (the epoch's fresh FastMCP snapshots
        the new route table); a failed build keeps the old epoch serving untouched.

        The build's fresh FastMCP lifespan is loop-affine, so the swap runs ON the
        serving loop even though this body is driven from a reload-gate worker thread.
        """
        env = self._config_manager.read_env()

        from tai42_skeleton.app.epoch import _reload_driven_by_request, build_and_swap_epoch

        # Read in THIS context (the reload-gate worker thread, into which
        # ``asyncio.to_thread`` copied the driving request's context): true when a
        # door request drove this reload, so the retire excuses that still-admitted
        # request instead of self-waiting the full drain budget on it. Captured here
        # and passed explicitly — ``_swap`` runs on the serving loop via
        # ``run_coroutine_threadsafe`` and does not inherit this thread's context.
        driven_by_request = _reload_driven_by_request.get()

        async def _swap() -> dict[str, Any]:
            # Release the loop-bound langgraph checkpoint/store pools before the
            # build's settings reset drops their per-loop registries — it refuses to
            # drop a registry still holding live resources on a running loop.
            await self._close_llm_registries()
            await build_and_swap_epoch(env, drain_tolerate_driver=driven_by_request)
            return {"status": "ok", "env_keys": len(env)}

        loop = self._serving_loop
        if loop is not None and loop.is_running():
            try:
                running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                raise RuntimeError("reload_config must be driven off the serving loop (via reload_gate.run)")
            # Marshal the swap onto the serving loop and block-wait its result.
            return asyncio.run_coroutine_threadsafe(_swap(), loop).result()
        # No serving loop bound (a pure-sync context / test): drive the swap on a
        # throwaway loop of this caller's own.
        return asyncio.run(_swap())

    async def _close_llm_registries(self) -> None:
        """Close the langgraph checkpoint + store resource pools — the release a
        settings reset requires before it can drop the per-loop registries."""
        await checkpoint_registry().close_all()
        await store_registry().close_all()

    def _live_mcp_status(self) -> dict[str, Any]:
        """Snapshot the in-process MCP-binding state.

        Returns ``{"bound": {title: [tool, ...]}, "failed": [{title, status}],
        "health": {title: {last_success, last_error, consecutive_failures,
        failing_since}}}`` (consumed by ``GET /api/mcp-status``). ``health`` covers
        every bound and failed title; a never-called MCP carries the block at its
        empty values. The four fields are this worker's passive dispatch health —
        per-process, so a fleet report reads each worker's own.

        Reads race a reload worker thread mutating ``_mcp_bound_tools`` (this
        read is deliberately not reload-gated, so status keeps answering
        mid-reload), so the dict and each per-title tool set are snapshot-copied
        — single C-level ops, atomic under the GIL — before iterating.
        """
        bound = {title: sorted(set(tools)) for title, tools in dict(self._mcp_bound_tools).items()}
        failed = self._list_failed_mcps()
        titles = set(bound) | {row["title"] for row in failed}
        return {
            "bound": bound,
            "failed": failed,
            "health": {title: mcp_health.snapshot(title) for title in sorted(titles)},
        }

    @abstractmethod
    def _mcp_tools(self, config: TaiMCPConfig, tools):
        raise NotImplementedError

    # Process-spine members the concrete subclass (``TaiMCP``) supplies; declared here
    # so this mixin's methods can reference them with a known type. The per-epoch
    # collaborators are the forwarding properties above, not declared here.
    _building: "ServingCore | None"
    _build_serving_core: Callable[[], "ServingCore"]
    clients: "ClientsFacet"
    preset_manager: "PresetManager"
    _config_manager: "ConfigManager"
    _mcp_sub_app_router: "SubMcpAppRouter"
