"""The worker-bus subscription lifecycle and fleet-op dispatch."""

import asyncio
import functools
import logging
import time
from typing import TYPE_CHECKING, Any, assert_never

from tai42_contract.app import tai42_app
from tai42_contract.backend import FleetOpApplied
from tai42_kit.settings import fold_env_name

from tai42_skeleton.app.boot_rules import BusNotDeploymentGivenError, check_bus_stays_deployment_given
from tai42_skeleton.app.bus import TRANSPORT_ERRORS, FleetResult, WorkerBus, WorkerKind, WorkerRow
from tai42_skeleton.app.bus.ops import (
    FLEET_OP_ADAPTER,
    ClearTemplateCache,
    DeploymentKeysChanged,
    DeregisterMcp,
    EvictTemplate,
    FleetOp,
    ListFailedMcps,
    Recycle,
    ReloadConfig,
    ReloadFailedMcps,
    ReloadMcp,
    ReloadTool,
    RemoveTool,
    ReprobeFailedMcps,
)
from tai42_skeleton.app.bus_settings import bus_settings
from tai42_skeleton.app.graceful_exit import request_graceful_exit
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.readiness_sentinel import write_ready_sentinel
from tai42_skeleton.app.recycle import RecycleError, RecycleReport, orchestrate_recycle
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.recycle_policy import CENSUS_TARGET_KINDS, _recycle_step_timeout

if TYPE_CHECKING:
    from tai42_skeleton.config.deployment_env import DeploymentOwnership

logger = logging.getLogger(__name__)


def _set_by_withdrawn(keys: list[str], withdrawn: list[str], ownership: "DeploymentOwnership") -> list[str]:
    """The keys of ``keys`` a withdrawn name set, in order.

    A withdrawn name sets a key when it is the same name under any spelling, or when it sets a
    field the key sets (a nested group's JSON name, a delimited name), as the index of env
    readers the withdraw's ``ownership`` judged by resolves both
    (:meth:`~tai42_skeleton.config.deployment_env.DeploymentOwnership.fields_set_by`).
    """
    folded = {fold_env_name(name) for name in withdrawn}
    fields = {ref for name in withdrawn for ref in ownership.fields_set_by(name)}
    return [key for key in keys if fold_env_name(key) in folded or not fields.isdisjoint(ownership.fields_set_by(key))]


# The name of the one long-lived bus subscription task; ``/ready`` names it once it dies, or once
# the subscription learns this process's bus is not the deployment's.
BUS_SUBSCRIPTION_TASK = "tai-worker-bus-subscription"


class BusSubscriptionMixin(LifecycleState):
    """Lifecycle mixin owning the worker-bus subscription and fleet-op dispatch."""

    @property
    def bus(self) -> WorkerBus:
        """This process's worker bus, built by the boot read (``read_boot_manifest``).

        The runtime-op publishers and the fleet census route reach the fleet through it. Raises if accessed
        before the boot read builds it.
        """
        bus = self._bus
        if bus is None:
            raise RuntimeError("the worker bus is not built — call read_boot_manifest first")
        return bus

    def _build_bus(self, kind: WorkerKind) -> WorkerBus:
        """Construct this process's one worker bus.

        With ``TAI_BUS_REDIS_URL`` set the real bus joins the fleet (its slot name + generation minted on
        the first claim at subscribe time); otherwise the no-op ``WorkerBus.local`` variant — legal only
        under the boot rules that permit a busless deployment (single worker, file mode, no backend).
        """
        settings = bus_settings()
        if settings.enabled:
            return WorkerBus(settings, kind=kind)
        return WorkerBus.local(kind)

    def _spawn_bus_subscription(self) -> None:
        """Start the one long-lived bus subscription on the serving loop.

        Owned by ``app_context``; runs until cancelled at shutdown. The subscription reconnects with backoff
        internally and fires ``on_ready`` (the self-resync) after subscribe+presence-register on every
        (re)connect.
        """
        bus = self._bus
        if bus is None:
            raise RuntimeError("bus subscription spawned before the bus was built")
        self._bus_subscription_task = asyncio.create_task(
            bus.subscribe(self._apply_bus_op, on_ready=self._resync_on_ready),
            name=BUS_SUBSCRIPTION_TASK,
        )
        self._bus_subscription_task.add_done_callback(self._on_perpetual_task_done)

    async def _resync_on_ready(self) -> None:
        """Self-resync run on every (re)connect, after subscribe and after the presence row is written as ``resyncing``.

        A local ``reload_config`` re-reads persisted state so a broadcast missed while this worker was away
        self-heals. Routed through the same apply path as a delivered op so ``on_fleet_op_applied`` handlers
        fire — else a reconnecting celery worker would resync only its main process while its prefork
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
        forking against a registry a broken reload left half-built.

        The deployment's ownership holds across the fleet. On every connect the resync first
        reads the census and withdraws the applied keys another worker's deployment sets
        (:meth:`_withdraw_fleet_owned_env`; a census it cannot read fails the resync). A
        reconnect then reloads persisted config under the names it just read; the first
        connect reloads only when it withdrew a key, so nothing it built keeps the withdrawn
        value. After every successful resync the worker is boot-ready and starts its join
        (:meth:`_spawn_join`): its presence row publishes its own names, every other worker
        withdraws the applied keys they make owned, and the recycle-class keys a withdraw
        leaves running are rolled as a recycle, this worker's own last. The join runs as a
        task, never awaited here, so this worker's message loop answers the fleet's ops,
        another joiner's announcement among them, while its own announcement collects; a
        worker still resyncing at the announcement replies once its own resync returns. A
        withdraw that takes away a value locating this worker's bus ends the process instead:
        no reload, no boot-ready latch, no join.
        """
        try:
            # Every connect learns the fleet's names: they may have changed since the boot
            # bridge read them, or while a reconnecting worker was away.
            withdrawn, ownership = await self._withdraw_fleet_owned_env()
            self_recycle_keys: list[str] = []
            if self._boot_ready.is_set() or withdrawn:
                # A RECONNECT re-reads persisted config by building and swapping in a fresh
                # epoch, since a reload_config broadcast may have been missed while this worker
                # was away. On the FIRST connect the live config was JUST built by ``start()``,
                # so it is rebuilt only when the withdraw changed the env it was built under.
                result = await self._reload_after_withdraw()
                self_recycle_keys = _set_by_withdrawn(result["awaiting_respawn"], withdrawn, ownership)
                self._judge_resync(result, joining_recycle_keys=self_recycle_keys)
        except asyncio.CancelledError:
            raise
        except BusNotDeploymentGivenError:
            # Logged at ERROR with the exit already requested; no resync follows.
            return
        except Exception:
            logger.error(
                "worker bus: self-resync failed — subscription stays live, resync retried on the next reconnect",
                exc_info=True,
            )
        else:
            self._mark_boot_ready()
            self._spawn_join(self_recycle_keys)

    async def _withdraw_fleet_owned_env(self) -> tuple[list[str], "DeploymentOwnership"]:
        """Withdraw the applied stored keys the fleet's deployment owns, read from the census under the reload gate.

        Returns the withdrawn names, sorted, and the ownership they were judged by. A failed
        census read raises. A withdraw that itself
        moves the effective bus settings off the bus this process is a member of (a value locating
        it, its Redis URL or namespace, was a withdrawn copy:
        :func:`~tai42_skeleton.app.boot_rules.check_bus_stays_deployment_given`) ends the
        process: the refusal is logged at ERROR, ``/ready`` names the bus subscription as dead,
        a graceful exit is requested for the supervisor to start the process again (its boot
        then refuses with the same text), and the :class:`BusNotDeploymentGivenError` is raised.
        """
        from tai42_skeleton.app.applied_env import withdraw_fleet_owned_env
        from tai42_skeleton.config.deployment_env import fleet_deployment_ownership

        bus = self._bus
        if bus is None:
            raise RuntimeError("the fleet ownership withdraw ran before the bus was built")
        try:
            async with reload_gate.synchronized():
                ownership = await fleet_deployment_ownership(bus)
                before = bus_settings()
                withdrawn = withdraw_fleet_owned_env(ownership)
                if withdrawn:
                    check_bus_stays_deployment_given(
                        member=bus.settings, before=before, after=bus_settings(), withdrawn=withdrawn
                    )
        except BusNotDeploymentGivenError as exc:
            logger.error(  # noqa: TRY400 a refusal outcome with its reason, not an unexpected error — no traceback
                "worker bus: %s — this process exits for its supervisor to start it again", exc
            )
            self._dead_perpetual_task = (BUS_SUBSCRIPTION_TASK, type(exc).__name__)
            request_graceful_exit(bus.identity.pid)
            raise
        return withdrawn, ownership

    async def _reload_after_withdraw(self) -> dict[str, Any]:
        """Rebuild this worker's serving generation through the fleet-op apply path and return the reload's result.

        Applied as a ``reload_config`` op, so the ``on_fleet_op_applied`` handlers fire with its
        registry effect (a backend turns its pool over) and nothing built under the old env
        keeps it.
        """
        return await self._apply_fleet_op(
            ReloadConfig(op="reload_config"), deadline=time.monotonic() + bus_settings().apply_timeout
        )

    async def _converge_after_withdraw(self, withdrawn: list[str], ownership: "DeploymentOwnership") -> dict[str, Any]:
        """Bring this worker's running state in line with a withdraw and report what still waits on a respawn.

        Returns ``{"withdrawn": [...], "awaiting_respawn": [...]}``: the withdrawn names, and the
        recycle-class keys a withdrawn name set that this worker still runs on (bound at boot, so
        only a respawn ends them), judged by the withdraw's ``ownership`` (:func:`_set_by_withdrawn`).
        A withdraw of nothing rebuilds nothing. A receiver whose withdraw took away a value
        locating its bus never reaches here: the withdraw raises, the op fails with the refusal's
        text, and the process exits.
        """
        if not withdrawn:
            return {"withdrawn": [], "awaiting_respawn": []}
        result = await self._reload_after_withdraw()
        return {
            "withdrawn": withdrawn,
            "awaiting_respawn": _set_by_withdrawn(result["awaiting_respawn"], withdrawn, ownership),
        }

    def _spawn_join(self, self_recycle_keys: list[str]) -> None:
        """Start this worker's join (:meth:`_join`) as a task, tracked until it ends and cancelled at shutdown."""
        task = asyncio.create_task(self._join(list(self_recycle_keys)), name="tai-deployment-keys-join")
        self._deployment_keys_join_tasks.add(task)
        task.add_done_callback(self._deployment_keys_join_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    async def _join(self, self_recycle_keys: list[str]) -> None:
        """Announce this worker's deployment keys, then recycle the workers whose withdraw left keys running.

        The roll (:meth:`_recycle_after_join`) runs when a worker replied with such keys, or
        when ``self_recycle_keys`` (this worker's own withdraw's recycle-class keys) is
        non-empty, and then recycles this worker last.
        """
        announced, reporters = await self._announce_deployment_keys()
        if reporters or self_recycle_keys:
            await self._recycle_after_join(announced, reporters, self_recycle_keys)

    async def _announce_deployment_keys(self) -> tuple[FleetResult | None, dict[str, list[str]]]:
        """Tell every other worker this one joined, so each withdraws the applied keys its names make owned.

        The announcement carries no names: each receiver re-reads the census, where this
        worker's presence row publishes them, and replies with the recycle-class keys its
        withdraw left running. Returns the announcement's result (``None`` when the publish
        failed) and those keys by the name of the worker that replied with them. A failed
        publish, or a worker that did not confirm, is logged at ERROR and leaves the
        subscription live: this join rolls no such worker. A hot key it runs on converges at
        its next reload, which applies by the fleet's ownership; a recycle-class key it still
        runs on is named at each reload and waits for a restart through the deployment's
        supervisor.
        """
        from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
        from tai42_skeleton.operations._broadcast import _awaiting_keys

        bus = self._bus
        if bus is None:
            raise RuntimeError("the deployment keys announcement ran before the bus was built")
        try:
            result = await bus.publish(
                DeploymentKeysChanged(op="deployment_keys_changed"),
                targets=None,
                local=LocalApplyResult(outcome=OpOutcome.applied),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "worker bus: announcing this worker's deployment keys to the fleet failed — this join rolls no "
                "other worker: a hot key converges at each worker's next reload, a recycle-class key a worker still "
                "runs on is named at each reload and waits for a restart through the deployment's supervisor",
                exc_info=True,
            )
            return None, {}
        if not result.ok:
            logger.error(
                "worker bus: not every worker confirmed this worker's deployment keys announcement — this join "
                "does not roll an unconfirmed worker: a hot key converges at its next reload, a recycle-class key "
                "it still runs on is named at each reload and waits for a restart through the deployment's "
                "supervisor: %s",
                result.model_dump_json(),
            )
        reporters = {
            row.name: keys
            for row in result.results
            if row.name != bus.identity.name and (keys := _awaiting_keys(row.payload))
        }
        return result, reporters

    async def _recycle_after_join(
        self, announced: FleetResult | None, reporters: dict[str, list[str]], self_recycle_keys: list[str]
    ) -> None:
        """Recycle the workers whose withdraw left recycle-class keys running, one at a time, this worker last.

        The targets are pinned from one census read, filtered to ``reporters``; a census the bus
        cannot answer pins nothing (WARNING), and the roll then reads the census itself and stops
        on the outage. The roll recycles exactly the pinned workers, kind by kind
        (``pinned_only``). A roll that stops is logged at ERROR with its partial report and
        this worker does not recycle itself; a raise with no recorded stop is a bus contract
        violation and escapes. A converged roll is logged at INFO; any worker it did not
        recycle is named as still awaiting a respawn; then, when ``self_recycle_keys`` is
        non-empty, this worker marks itself recycling and exits gracefully for its supervisor
        to start it again.
        """
        from tai42_skeleton.app.bus import OpOutcome, WorkerResult
        from tai42_skeleton.config.service.broadcast import _log_stopped_roll
        from tai42_skeleton.operations._broadcast import awaiting_respawn_after, log_awaiting_respawn, recycle_roll

        bus = self.bus
        identity = bus.identity
        change = f"deployment keys of {identity.name}"
        try:
            rows: list[WorkerRow] | None = await bus.census()
        except TRANSPORT_ERRORS:
            logger.warning(
                "recycle targets for %r: the census could not be read — no target pinned, the roll reports the "
                "bus as unreachable",
                change,
                exc_info=True,
            )
            rows = None
        pinned = (
            {}
            if rows is None
            else {
                row.name: (row.kind, row.generation)
                for row in rows
                if row.name in reporters and row.kind in CENSUS_TARGET_KINDS and row.name != identity.name
            }
        )
        target_kinds = (
            CENSUS_TARGET_KINDS
            if rows is None
            else tuple(kind for kind in CENSUS_TARGET_KINDS if any(k == kind for k, _ in pinned.values()))
        )
        # The reporters' payloads, plus this worker's own keys, judged after the roll.
        fleet = FleetResult(
            op="deployment_keys_changed",
            results=[
                WorkerResult(
                    name=identity.name, outcome=OpOutcome.applied, payload={"awaiting_respawn": self_recycle_keys}
                ),
                *(row for row in (announced.results if announced is not None else []) if row.name != identity.name),
            ],
        )
        self_exit = bool(self_recycle_keys)
        try:
            report: RecycleReport = await orchestrate_recycle(
                bus,
                excluded_name=identity.name,
                applier_generation=identity.generation,
                target_kinds=target_kinds,
                applier_self_deferred=self_exit,
                step_timeout=_recycle_step_timeout(),
                expected=pinned,
                pinned_only=True,
            )
        except RecycleError as exc:
            stopped = exc.report.stopped
            if stopped is None:
                raise
            _log_stopped_roll(exc.report, stopped, pinned, change=change)
            log_awaiting_respawn(awaiting_respawn_after(fleet, exc.report, identity, self_exit_armed=False))
            return
        logger.info("recycle for %s converged: %s", change, recycle_roll(report, identity, self_exit_armed=self_exit))
        log_awaiting_respawn(awaiting_respawn_after(fleet, report, identity, self_exit_armed=self_exit))
        if self_exit:
            logger.info(
                "worker bus: this worker still runs on withdrawn recycle-class key(s) %s — recycling itself",
                ", ".join(self_recycle_keys),
            )
            await bus.mark_recycling()
            request_graceful_exit(identity.pid)

    def _judge_resync(self, result: Any, *, joining_recycle_keys: list[str]) -> None:
        """Judge this worker's own resync reload: nothing in a resync respawns it.

        The resyncing worker is the operation that started the reload, so the recycle-class
        keys its reload states as moved off their bound value (a roll it missed while away, a stopped
        roll that never reached it) are its own verdict, logged loudly. The
        ``joining_recycle_keys`` its own withdraw left running are not: the join's recycle roll
        recycles this worker for them, and judges them itself.
        """
        from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerResult
        from tai42_skeleton.operations._broadcast import awaiting_respawn_after, log_awaiting_respawn

        bus = self._bus
        if bus is None:
            raise RuntimeError("bus resync ran before the bus was built")
        report = FleetResult(
            op="reload_config",
            results=[
                WorkerResult(
                    name=bus.identity.name,
                    outcome=OpOutcome.applied,
                    payload={
                        **result,
                        "awaiting_respawn": [
                            key for key in result["awaiting_respawn"] if key not in joining_recycle_keys
                        ],
                    },
                )
            ],
        )
        log_awaiting_respawn(awaiting_respawn_after(report, None, None, self_exit_armed=False))

    def _mark_boot_ready(self) -> None:
        """Latch the boot-ready signal on the FIRST successful self-resync.

        One-way: a reconnect resync re-enters here on a live app but must never clear the latch, so a
        consumer already past it is never retroactively un-readied.

        Writes the readiness sentinel BEFORE latching, so ``boot-ready`` and the
        sentinel the readiness probe tests are consistent: an unwritable sentinel path
        raises here (a loud boot fault), leaving the latch unset for the next reconnect
        to retry rather than reporting ready without the probe's signal.
        """
        if not self._boot_ready.is_set():
            write_ready_sentinel()
            logger.info("app boot-ready: first self-resync complete — tool registry built and stable")
            self._boot_ready.set()

    async def _wait_until_ready(self) -> None:
        """Back ``app.lifecycle.wait_until_ready``: block until the first boot self-resync has latched boot-ready."""
        await self._boot_ready.wait()

    async def _cancel_bus_subscription(self) -> None:
        """Cancel the join, if one runs, and the bus subscription, and await their termination.

        The shutdown counterpart of ``_spawn_bus_subscription``. A task that
        died with a non-``CancelledError`` exception was already surfaced at
        ERROR by its done-callback, so it is awaited-and-swallowed here rather
        than re-raised — this runs inside ``app_context``'s shutdown
        ``finally``, and one dead task must not skip the remaining teardown.
        """
        for join in list(self._deployment_keys_join_tasks):
            join.cancel()
        # A cancelled join ends silently; a join that raised was logged by its done-callback.
        await asyncio.gather(*self._deployment_keys_join_tasks, return_exceptions=True)
        task = self._bus_subscription_task
        self._bus_subscription_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 task failure already surfaced by the done-callback; swallowed so shutdown completes
            # Already logged at ERROR by the done-callback; swallowed so a dead
            # subscription cannot abort the remaining shutdown steps.
            pass

    async def _apply_bus_op(self, raw: dict[str, Any], *, deadline: float) -> Any:
        """Apply one fleet op delivered from a sibling worker.

        The delivered dict is validated into its typed op first: an unknown op or a missing or
        malformed field raises before anything applies, so the confirmation reports ``failed``
        and no partial op ever lands.
        """
        return await self._apply_fleet_op(FLEET_OP_ADAPTER.validate_python(raw), deadline=deadline)

    async def _apply_fleet_op(self, op: FleetOp, *, deadline: float) -> Any:
        """Apply one fleet op (delivered, or the self-resync), then fire the post-apply hooks.

        The sibling-worker counterpart of a route-received admin call: it maps each op
        to the local admin primitive. The heavy sync ops run on a worker thread through
        the reload gate, identically to the HTTP path; the tool reload/remove ops
        dispatch through the async per-kind reloader; ``reprobe_failed_mcps`` runs the
        set-scoped re-probe on the serving loop, taking the reload gate only to snapshot and
        bind; ``list_failed_mcps`` is a plain in-process read.

        After the op applies, ``on_fleet_op_applied`` handlers fire with the applied op — its
        declared effects and the seconds left before ``deadline`` (a ``time.monotonic()``
        instant). The op has not fully "applied" until they finish (a celery worker must
        re-fork its prefork pool before it reports applied), and a raising handler fails the
        op. The returned value becomes the op's terminal ``applied`` payload; the publisher
        echo-skips its own broadcast, so this never re-applies a self-op.
        """
        result = await self._dispatch_bus_op(op)
        applied = FleetOpApplied(op=op.op, effects=type(op).effects, budget=max(0.0, deadline - time.monotonic()))
        await self._run_fleet_op_applied_handlers(applied)
        return result

    async def _dispatch_bus_op(self, op: FleetOp) -> Any:
        """Route one fleet op to its local admin primitive and return its result."""
        match op:
            case ReloadConfig():
                return await reload_gate.run(tai42_app.admin.reload_config, reimports=True)
            case ReloadMcp(title=title):
                return await reload_gate.run(lambda: tai42_app.admin.reload_mcp(title), reimports=False)
            case DeregisterMcp(title=title):
                return await reload_gate.run(lambda: tai42_app.admin.deregister_mcp(title), reimports=False)
            case ReloadTool(kind=kind, name=name):
                return await tai42_app.admin.run_tool_reload(kind, "reload", name)
            case RemoveTool(kind=kind, name=name):
                return await tai42_app.admin.run_tool_reload(kind, "remove", name)
            case ReloadFailedMcps() | ReprobeFailedMcps() | ListFailedMcps():
                return await self._apply_failed_mcps_op(op)
            case EvictTemplate() | ClearTemplateCache():
                return self._apply_template_op(op)
            case Recycle():
                return await self._apply_recycle()
            case DeploymentKeysChanged():
                return await self._converge_after_withdraw(*await self._withdraw_fleet_owned_env())
            case _:
                assert_never(op)

    async def _apply_failed_mcps_op(self, op: ReloadFailedMcps | ReprobeFailedMcps | ListFailedMcps) -> Any:
        """Apply a failed-MCP fleet op on this worker and return the bare list its self-apply returns too.

        Every origin's payload in one FleetResult is then uniform: a consumer never special-cases
        self vs remote for these ops.
        """
        match op:
            case ReloadFailedMcps():
                return await reload_gate.run(tai42_app.admin.reload_failed_mcps, reimports=False)
            case ReprobeFailedMcps(titles=titles):
                # Runs on the serving loop: the pass takes the reload gate only to snapshot and to bind, never
                # across its probe.
                return await tai42_app.admin.reprobe_failed_mcps(titles)
            case ListFailedMcps():
                return tai42_app.admin.list_failed_mcps()
            case _:
                assert_never(op)

    def _apply_template_op(self, op: EvictTemplate | ClearTemplateCache) -> dict[str, Any]:
        """Apply a template-cache fleet op on this worker.

        Evict one rendered template (or a whole directory under a key) for ``evict_template``, else clear
        the entire compiled-template cache for ``clear_template_cache``.
        """
        resource_manager = tai42_app.storage.resource_manager
        if isinstance(op, ClearTemplateCache):
            resource_manager.clear_cache()
            return {"cleared": True}
        # The template store was written on the origin worker; drop this worker's stale compilation
        # so its next render reflects the new content. ``prefix`` marks a directory delete (evict
        # everything under the key).
        if op.prefix:
            resource_manager.evict_dir(op.path)
        else:
            resource_manager.evict_compiled(op.path)
        return {"evicted": op.path}

    async def _apply_recycle(self) -> dict[str, Any]:
        """Apply a targeted recycle op.

        Write ``state=recycling`` into presence, arm the bus's single-shot post-terminal-reply slot with
        this process's graceful self-exit, then return the applied payload.

        The recycling state is written BEFORE arming the self-SIGTERM — the only viable
        seam, since the graceful-exit callable is sync and a ``create_task`` there would
        race the SIGTERM — so the census shows WHY this worker is departing. The exit
        fires only AFTER the terminal ``applied`` reply ships, so the orchestrator
        records a successful recycle before this process departs. The payload names the
        graceful-exit kind only — never an env value.
        """
        identity = self.bus.identity
        await self.bus.mark_recycling()
        self.bus.arm_post_reply(functools.partial(request_graceful_exit, identity.pid))
        return {"recycling": identity.kind.value}
