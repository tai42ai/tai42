"""The lifecycle mixin's construction-time state and the concrete-subclass spine it relies on."""

import asyncio
import os
import sys
import weakref
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from tai42_contract.backend import FleetOpApplied
from tai42_contract.manifest import TaiMCPConfig
from tai42_contract.template import TemplateEviction

from tai42_skeleton.app.bus import WorkerBus, WorkerKind
from tai42_skeleton.app.lifecycle.drain import DrainBudgetRegistry
from tai42_skeleton.app.lifecycle.exit_drain import ExitDrain
from tai42_skeleton.app.mount_map import MountBinding
from tai42_skeleton.app.readiness import ReadinessRegistry

if TYPE_CHECKING:
    import mcp
    from fastmcp import FastMCP
    from tai42_contract.config.manager import ConfigManager

    from tai42_skeleton.agent.binding import AgentBinding
    from tai42_skeleton.app.bus.ops import CarryRoll
    from tai42_skeleton.app.clients import ClientsFacet
    from tai42_skeleton.app.http import HttpSurface
    from tai42_skeleton.app.lifecycle.carried_roll import HeldRoll
    from tai42_skeleton.app.server import ServingCore
    from tai42_skeleton.app.sessions import SessionRegistry
    from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
    from tai42_skeleton.backend.registry import BackendHolder
    from tai42_skeleton.backup import BackupRegistry
    from tai42_skeleton.channels.registry import ChannelRegistry
    from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
    from tai42_skeleton.extensions import ExtensionRegistry
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.marketplace.installed_packages import InstalledPackages
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
    from tai42_skeleton.storage.prefix_marks import PrefixMarkRegistry
    from tai42_skeleton.template import ResourceManager
    from tai42_skeleton.template.delete_referees import TemplateDeleteRefereeRegistry
    from tai42_skeleton.tools import (
        TemplateRefsRegistry,
        ToolExtrasRegistry,
        ToolPauseRegistry,
        ToolRefsRegistry,
        ToolRegistry,
        ToolRetryRegistry,
        ToolTierRegistry,
    )
    from tai42_skeleton.tools.binding import ToolBinding
    from tai42_skeleton.tools.delete_cascades import ToolDeleteCascadeRegistry
    from tai42_skeleton.tools.delete_referees import ToolDeleteRefereeRegistry
    from tai42_skeleton.tools.detach_referees import StateTemplateDetachRefereeRegistry
    from tai42_skeleton.tools.mcp_start import StartId
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
        # serving loop) — never by the per-epoch handler list, which runs while the
        # generation is still being built, before the retire cancels the previous
        # generation's loops. Keyed by
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
        # The join a resync starts: its deployment keys announcement and the recycle roll it
        # may run; tracked until it ends, and cancelled at shutdown.
        self._deployment_keys_join_tasks: set[asyncio.Task[None]] = set()
        # The exit a recycle op deferred to this worker's own in-flight changes runs once they are answered.
        self._deferred_recycle_task: asyncio.Task[bool] | None = None
        # The rolls bounded changes handed to this worker (the ``carry_roll`` op), each held until
        # the run that handed it off released it (the ``release_roll`` op), then carried out, as
        # its own task; tracked until it ends, and cancelled at shutdown.
        self._carried_roll_tasks: set[asyncio.Task[None]] = set()
        # Those rolls by their identity, from their acceptance until their task ended, so a
        # release finds the roll it starts, or reads that the roll had started already.
        self._held_rolls: dict[str, HeldRoll] = {}
        # Latched once this process's first boot self-resync completes: the tool
        # registry is then fully built and stable for the run. A forking/consuming
        # backend runtime awaits this (``wait_until_ready``) before its work loop
        # accepts a job, so a work-horse forked at boot never inherits a half-built
        # registry. One-way for the process lifetime — a reconnect self-resync
        # re-runs while the app is already live and must not un-ready it — so it is
        # NOT reset by ``start()``/reload.
        self._boot_ready = asyncio.Event()
        # The loop this process serves from, bound by ``app_context`` and read only through
        # :meth:`serving_loop` (:meth:`bind_serving_loop`, :meth:`unbind_serving_loop`).
        self._serving_loop: asyncio.AbstractEventLoop | None = None

        # Set once a perpetual task dies (an exception OR an unexpected clean
        # return — cancellation at shutdown is not death): ``(task name, reason)``
        # where the reason is the exception TYPE name or the literal ``"returned"``.
        # ``/ready`` reads it through :meth:`dead_perpetual_task` and 503s naming
        # the task; the done-callback that sets it also requests this process's
        # graceful exit. The bus subscription also sets it, naming itself, when a
        # withdraw takes away a value locating this process's bus
        # (``BusNotDeploymentGivenError``), and requests the exit the same way.
        self._dead_perpetual_task: tuple[str, str] | None = None

        # Handlers fired with the OP NAME after every applied bus op AND after the
        # reconnect self-resync reload. Keyed by qualified name so a module re-import
        # replaces rather than accumulates (mirrors the startup/reload registries). A
        # backend whose worker model needs post-op work registers here (celery's
        # prefork pool turnover is the known consumer).
        self._fleet_op_applied_handlers: dict[str, Callable] = {}

        # Handlers fired, synchronously, with every ``TemplateEviction`` the resource
        # manager applies. Keyed by qualified name, like the fleet-op registry.
        self._template_evicted_handlers: dict[str, Callable[[TemplateEviction], Any]] = {}

        # Handlers fired, synchronously, once the cold-boot env bridge has applied the stored
        # env: each re-applies a process-wide effect its owner set up from settings read
        # before the bridge (the root logger level, fastmcp's settings), so the
        # effect follows the applied env. Keyed by qualified name, like the registries above.
        self._boot_env_handlers: dict[str, Callable[[], Any]] = {}

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

        # The inbound-media retention reaper loop, owned by app_context: the sole deleter
        # of an ingested blob and its metadata past their horizon (an abandoned pending
        # upload, an expired bound record). A no-op each pass absent a store/provider;
        # runs until cancelled at shutdown.
        self._media_reaper_task: asyncio.Task[None] | None = None

        # The monitoring health watch, owned by app_context: raises one platform event per
        # growth of the run family's monitoring delivery-failure counters. Runs until
        # cancelled at shutdown.
        self._monitoring_health_watch_task: asyncio.Task[None] | None = None

        # The module → mount-binding map for the CURRENT registration pass, rebuilt at
        # the top of ``_initialize_components`` before any manifest module imports.
        self._mount_map: dict[str, MountBinding] = {}

        # The backing stores ``/ready`` pings and the drain budget a retire or recycle
        # waits for, as each subsystem declares them on this app. Owned by the app, so a
        # rebuilt app declares afresh and a failed build leaves nothing behind.
        self._readiness = ReadinessRegistry()
        self._drain_budgets = DrainBudgetRegistry()
        # How long this process may take to finish its in-flight work and exit once asked to
        # stop, declared by the runtime that owns its stop and the modules whose shutdown steps
        # wait a grace; the bus announces it on leaving.
        self._exit_drain = ExitDrain()

    def dead_perpetual_task(self) -> tuple[str, str] | None:
        """The ``(task name, reason)`` of a perpetual task that died, or ``None``.

        The bus subscription also names itself here, with ``BusNotDeploymentGivenError`` as the
        reason, once it learns this process's bus is not the deployment's.

        ``/ready`` reads it here rather than the private attribute so a dead
        perpetual task surfaces as a named readiness failure.
        """
        return self._dead_perpetual_task

    def serving_loop(self) -> asyncio.AbstractEventLoop | None:
        """The loop this process serves from while ``app_context`` holds it bound, else ``None``.

        ``None`` before the boot binds it, once ``app_context`` has left, and in a forked child,
        which serves from no loop of its own (:func:`_unbind_serving_loops_after_fork`).
        """
        return self._serving_loop

    def bind_serving_loop(self) -> None:
        """Bind the running loop as this process's serving loop (``app_context``, once the boot is up)."""
        self._serving_loop = asyncio.get_running_loop()
        _BOUND_SERVING_LOOPS.add(self)

    def unbind_serving_loop(self) -> None:
        """Unbind the serving loop (``app_context`` leaving, or a forked child)."""
        self._serving_loop = None
        _BOUND_SERVING_LOOPS.discard(self)

    def on_serving_loop(self) -> bool:
        """Whether the caller runs on this process's serving loop.

        A task spawned there runs for the process lifetime; a task left on any other loop (a
        backend worker's private task loop) may never run again.
        """
        serving = self.serving_loop()
        return serving is not None and serving is asyncio.get_running_loop()

    @property
    def readiness(self) -> ReadinessRegistry:
        """The readiness contributors this app's subsystems declare; ``/ready`` reads them here."""
        return self._readiness

    @property
    def drain_budgets(self) -> DrainBudgetRegistry:
        """The drain budgets this app's modules declare; a retire and a recycle step read them here."""
        return self._drain_budgets

    @property
    def exit_drain(self) -> ExitDrain:
        """This process's exit drain; the bus announces it on leaving.

        Declared by the runtime that owns its stop and the modules whose shutdown steps wait a grace.
        """
        return self._exit_drain

    @abstractmethod
    def _mcp_tools(self, config: TaiMCPConfig, tools, preset_names: frozenset[str]):
        raise NotImplementedError

    if TYPE_CHECKING:
        # Signatures of the methods invoked across concern-mixin boundaries, declared
        # so each mixin's ``self.<method>()`` reference resolves; the concrete
        # implementations live on the owning mixins the composite class inherits.
        async def _run_handlers(self, handlers: list[Callable], raise_on_error: bool = False): ...
        async def _run_post_swap_handlers(self, *, raise_on_error: bool) -> None: ...
        async def _run_fleet_op_applied_handlers(self, applied: FleetOpApplied) -> None: ...
        def _fire_template_evicted(self, eviction: TemplateEviction) -> None: ...
        def _fire_boot_env_applied(self) -> None: ...
        def _epoch_handlers(self) -> list[Callable]: ...
        def _refresh_manifest_mcp(self) -> None: ...
        @property
        def _serving_core(self) -> "ServingCore": ...
        @property
        def _live_serving_core(self) -> "ServingCore": ...
        def _require_manifest(self) -> "Manifest": ...
        def _require_live_manifest(self) -> "Manifest": ...
        def _on_perpetual_task_done(self, task: asyncio.Task[Any]) -> None: ...
        @staticmethod
        async def _cancel_perpetual_task(task: asyncio.Task[Any] | None) -> None: ...
        @staticmethod
        def _log_task_exception(task: asyncio.Task[Any]) -> bool: ...
        @property
        def bus(self) -> WorkerBus:
            """This process's worker bus (implemented on the bus-subscription mixin)."""
            ...

        def _build_bus(self, kind: WorkerKind) -> WorkerBus: ...
        def _spawn_bus_subscription(self) -> None: ...
        async def _cancel_bus_subscription(self) -> None: ...
        async def accept_carried_roll(self, op: CarryRoll, *, answers_within: float | None) -> dict[str, str]:
            """Accept the roll ``op`` hands this worker (implemented on the carried-roll mixin)."""
            ...

        def _release_carried_roll(self, roll: str, change: str) -> dict[str, str]: ...
        async def _cancel_carried_rolls(self) -> None: ...
        async def start(self, manifest: "Manifest"):
            """Boot the app from ``manifest`` (implemented on the boot mixin)."""
            ...

        async def _initialize_components(self, installed: "InstalledPackages") -> None: ...
        def _effective_router_modules(self) -> list[str]: ...
        async def _build_mount_map(self, installed: "InstalledPackages") -> dict[str, MountBinding]: ...
        async def _probe_mcp(self, config: TaiMCPConfig, *, wait: float | None = None) -> list["mcp.types.Tool"]: ...
        async def _load_mcps(
            self,
        ) -> tuple[list[tuple[TaiMCPConfig, Any]], list[tuple[TaiMCPConfig, BaseException]]]: ...
        def _record_failed_mcp(self, config: TaiMCPConfig, exc: BaseException) -> None: ...
        async def _bring_listed_credentials_up_to_date(self) -> dict[str, bool]: ...
        async def _bring_boot_credentials_up_to_date(self) -> None: ...
        def _missing_tools_ignore(self) -> frozenset[str]: ...
        async def _apply_reloaded_mcp(self, title: str, config: TaiMCPConfig, tools: list[Any]) -> dict[str, Any]: ...
        async def _apply_probe_outcome(
            self, title: str, config: TaiMCPConfig, outcome: Any, *, awaited: "StartId | None"
        ) -> dict[str, Any] | None: ...
        def _start_awaited_by(self, title: str) -> "StartId | None": ...
        async def _reprobe_failed_titles(self, titles: Iterable[str]) -> list[dict[str, Any]]: ...
        def _spawn_reprobe_task(self) -> None: ...
        async def _cancel_reprobe_task(self) -> None: ...

    # Process-spine members the concrete subclass (``TaiMCP``) supplies; declared here
    # so the concern mixins' methods can reference them with a known type.
    _scaffold_core: "ServingCore | None"
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
    _failed_mcps: dict[str, dict[str, Any]]
    _mcp_bound_tools: dict[str, set[str]]
    _mcp_listed_tools: dict[str, list["mcp.types.Tool"]]
    _mcp_preset_conflicts: dict[str, set[str]]
    _mcp_unusable_tools: dict[str, set[str]]
    _mcp_credential_demanded: set[str]
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
    _prefix_mark_registry: "PrefixMarkRegistry"
    _write_validator_registry: "PresetWriteValidatorRegistry"
    _target_validator_registry: "TargetBindValidatorRegistry"
    _input_schema_support_registry: "PresetInputSchemaSupportRegistry"
    _registration_tier_registry: "ToolTierRegistry"
    _tool_refs_registry: "ToolRefsRegistry"
    _template_refs_registry: "TemplateRefsRegistry"
    _tool_retry_registry: "ToolRetryRegistry"
    _tool_extras_registry: "ToolExtrasRegistry"
    _tool_pause_registry: "ToolPauseRegistry"
    _rename_referee_registry: "ToolRenameRefereeRegistry"
    _delete_referee_registry: "ToolDeleteRefereeRegistry"
    _delete_cascade_registry: "ToolDeleteCascadeRegistry"
    _detach_referee_registry: "StateTemplateDetachRefereeRegistry"
    _template_delete_referee_registry: "TemplateDeleteRefereeRegistry"
    _seed_registry: "PresetSeedRegistry"
    _backup_registry: "BackupRegistry"
    _states_service: "StatesService"
    _states_attach_validators: "StatesAttachValidatorRegistry"
    _states_attach_reconcilers: "StatesAttachReconcilerRegistry"
    _states_consumer_listers: "StatesConsumerListerRegistry"
    _states_template_seeds: "StateTemplateSeedRegistry"
    _preset_manager: "PresetManager"


# Every lifecycle whose serving loop is bound, so a forked child unbinds them all: the child inherits
# the parent's loop object, which reads as running there though no thread of the child runs it.
_BOUND_SERVING_LOOPS: "weakref.WeakSet[LifecycleState]" = weakref.WeakSet()


def _unbind_serving_loops_after_fork() -> None:
    """In a forked child, unbind every serving loop the parent had bound."""
    for state in list(_BOUND_SERVING_LOOPS):
        state.unbind_serving_loop()


if sys.platform != "win32":
    os.register_at_fork(after_in_child=_unbind_serving_loops_after_fork)


class _ServingLoopTask[R]:
    """Work run as a task of the serving loop, its end reported to the caller's loop.

    Created on the caller's loop, it schedules the task on ``serving`` in the caller's context.
    :attr:`ended` resolves, on the caller's loop, to the finished task once the task is done,
    whether it returned, raised, or was cancelled, before or after it started.
    """

    # Set by ``_start`` on the serving loop, which runs it before any ``_cancel`` scheduled after it.
    _task: asyncio.Task[R]

    def __init__(self, fn: Callable[[], Awaitable[R]], serving: asyncio.AbstractEventLoop) -> None:
        """Schedule ``fn`` as a task of ``serving``; ``ended`` resolves on the running loop."""
        self._caller = asyncio.get_running_loop()
        self._serving = serving
        self.ended: asyncio.Future[asyncio.Task[R]] = self._caller.create_future()
        serving.call_soon_threadsafe(self._start, fn)

    def cancel(self) -> None:
        """Ask the serving loop to cancel the task; scheduled after its start, so the task exists by then."""
        self._serving.call_soon_threadsafe(self._cancel)

    def _start(self, fn: Callable[[], Awaitable[R]]) -> None:
        async def work() -> R:
            return await fn()

        self._task = self._serving.create_task(work())
        self._task.add_done_callback(self._report_end)

    def _cancel(self) -> None:
        self._task.cancel()

    def _report_end(self, task: asyncio.Task[R]) -> None:
        self._caller.call_soon_threadsafe(self.ended.set_result, task)


async def run_on_serving_loop[R](serving: asyncio.AbstractEventLoop | None, fn: Callable[[], Awaitable[R]]) -> R:
    """Await ``fn`` on the serving loop ``serving`` in the caller's context; its result and exception reach the caller.

    On the serving loop, or with none bound (``None``: a bare caller, a forked child, a boot
    outside ``app_context``), ``fn`` is awaited directly. From any other loop it is handed to the
    serving loop as a task and awaited there without blocking the caller's loop. Cancelling the
    caller cancels that task, and the caller returns only once the task has ended on the serving
    loop, its cleanup done: further cancels while it unwinds are held, then the cancellation is
    re-raised, or the task's own failure while unwinding, with the cancellation as its
    ``__context__``. A bound serving loop that is not running raises rather than be handed work
    it would never run.
    """
    if serving is None or serving is asyncio.get_running_loop():
        return await fn()
    if not serving.is_running():
        raise RuntimeError("the serving loop is bound but not running, so the work handed to it cannot run")

    hop = _ServingLoopTask(fn, serving)
    cancelled: asyncio.CancelledError | None = None
    while not hop.ended.done():
        try:
            # ``wait`` neither cancels ``ended`` nor raises: it only returns once the task is done.
            await asyncio.wait((hop.ended,))
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
                hop.cancel()
    task = hop.ended.result()
    if cancelled is None:
        return task.result()
    if not task.cancelled() and (failure := task.exception()) is not None:
        failure.__context__ = cancelled
        raise failure
    raise cancelled
