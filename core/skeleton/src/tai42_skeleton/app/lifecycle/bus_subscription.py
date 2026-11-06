"""The worker-bus subscription lifecycle and fleet-op dispatch."""

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any, assert_never

from tai42_contract.app import tai42_app
from tai42_contract.backend import FleetOpApplied

from tai42_skeleton.app.boot_rules import BusNotDeploymentGivenError, check_bus_stays_deployment_given
from tai42_skeleton.app.bus import FleetResult, WorkerBus, WorkerKind
from tai42_skeleton.app.bus.ops import (
    FLEET_OP_ADAPTER,
    CarryRoll,
    ClearTemplateCache,
    DeploymentKeysChanged,
    DeregisterMcp,
    EvictTemplate,
    FleetOp,
    ListFailedMcps,
    Recycle,
    ReleaseRoll,
    ReloadConfig,
    ReloadFailedMcps,
    ReloadMcp,
    ReloadTool,
    RemoveTool,
    ReprobeFailedMcps,
    recycle_reply,
)
from tai42_skeleton.app.bus_settings import bus_settings
from tai42_skeleton.app.discarded_build import discarded_build_statement
from tai42_skeleton.app.lifecycle.config_reload import awaiting_respawn
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.readiness_sentinel import write_ready_sentinel
from tai42_skeleton.app.recycle import orchestrate_recycle
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.writer_exit import hold_writer_exit, writer_exit_scope

logger = logging.getLogger(__name__)


# The name of the one long-lived bus subscription task; ``/ready`` names it once it dies, or once
# the subscription learns this process's bus is not the deployment's.
BUS_SUBSCRIPTION_TASK = "tai-worker-bus-subscription"

# The reason a worker recycled by a fleet ``Recycle`` op leaves for, as a later door reads it.
_RECYCLE_OP_REASON = "a fleet recycle op"


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

    def stop_began(self, reason: str) -> bool:
        """Begin this worker's leaving because its process was asked to stop, from outside, for ``reason``.

        The process's host calls it at the first stop signal it observes, so the census reads the
        stop as ``recycling`` from its start and a roll that reaches the worker meanwhile finds it
        already leaving (:meth:`~tai42_skeleton.app.bus.WorkerBus.stop_began`). It sends no signal:
        the stop is already under way. A signal that arrives before the boot read builds the bus
        begins nothing on it, said at INFO; the lifespan stop then has no bus row to announce
        either. Returns whether this call began the leaving.
        """
        bus = self._bus
        if bus is None:
            logger.info("%s before the worker bus was built — no census row to announce the stop on", reason)
            return False
        return bus.stop_began(reason)

    def _build_bus(self, kind: WorkerKind) -> WorkerBus:
        """Construct this process's one worker bus.

        With ``TAI_BUS_REDIS_URL`` set the real bus joins the fleet (its slot name + generation minted on
        the first claim at subscribe time), announcing this app's :attr:`exit_drain` when it leaves;
        otherwise the no-op ``WorkerBus.local`` variant — legal only under the boot rules that permit
        a busless deployment (single worker, file mode, no backend).
        """
        settings = bus_settings()
        if settings.enabled:
            return WorkerBus(settings, kind=kind, exit_drain=self.exit_drain.seconds)
        return WorkerBus.local(kind)

    def _spawn_bus_subscription(self) -> None:
        """Start the one long-lived bus subscription on the serving loop.

        Owned by ``app_context``; runs until cancelled at shutdown. The subscription reconnects with backoff
        internally and fires ``on_ready`` (the self-resync) after subscribe+presence-register on every
        (re)connect. A delivered op that fails replies ``failed`` with the statement
        :func:`_failed_op_statement` makes of its failure.
        """
        bus = self._bus
        if bus is None:
            raise RuntimeError("bus subscription spawned before the bus was built")
        self._bus_subscription_task = asyncio.create_task(
            bus.subscribe(self._apply_bus_op, on_ready=self._resync_on_ready, failure_payload=_failed_op_statement),
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
        withdraws the applied keys they make owned, and the join decides from their replies
        and this worker's own resync reload which workers need a restart, rolling the recycle
        over them, this worker's own last: a recycle-class key the resync reports off its bound
        value (a withdraw, a roll it missed while away) restarts this worker as it does any. The join runs as a
        task, never awaited here, so this worker's message loop answers the fleet's ops,
        another joiner's announcement among them, while its own announcement collects; a
        worker still resyncing at the announcement replies once its own resync returns. A
        withdraw that takes away a value locating this worker's bus ends the process instead:
        no reload, no boot-ready latch, no join.
        """
        try:
            # Every connect learns the fleet's names: they may have changed since the boot
            # bridge read them, or while a reconnecting worker was away.
            withdrawn = await self._withdraw_fleet_owned_env()
            # The FIRST connect's live config was JUST built by ``start()`` under the env it
            # runs on, so with nothing withdrawn it is clean and not rebuilt.
            own_reload: dict[str, Any] = {"awaiting_respawn": []}
            if self._boot_ready.is_set() or withdrawn:
                # A RECONNECT re-reads persisted config by building and swapping in a fresh
                # epoch, since a reload_config broadcast may have been missed while this worker
                # was away; a first connect rebuilds when the withdraw changed its env.
                own_reload = await self._reload_after_withdraw()
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
            self._spawn_join(own_reload)

    async def _withdraw_fleet_owned_env(self) -> list[str]:
        """Withdraw the applied stored keys the fleet's deployment owns, read from the census under the reload gate.

        Returns the withdrawn names, sorted. A failed census read raises. A withdraw that itself
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
            bus.request_graceful_exit("bus settings are no longer deployment-given")
            raise
        return withdrawn

    async def _reload_after_withdraw(self) -> dict[str, Any]:
        """Rebuild this worker's serving generation through the fleet-op apply path and return the reload's result.

        Applied as a ``reload_config`` op, so the ``on_fleet_op_applied`` handlers fire with its
        registry effect (a backend turns its pool over) and nothing built under the old env
        keeps it.
        """
        return await self._apply_fleet_op(
            ReloadConfig(op="reload_config"), deadline=time.monotonic() + bus_settings().apply_timeout
        )

    async def _converge_after_withdraw(self, withdrawn: list[str]) -> dict[str, Any]:
        """Bring this worker's running state in line with a withdraw and report what still waits on a respawn.

        Returns ``{"withdrawn": [...], "awaiting_respawn": [...]}``: the withdrawn names, and every
        recycle-class key this worker still runs on off its effective value (bound at boot, so only
        a respawn ends them), whatever change set it — the same statement every reload makes. A
        withdraw of nothing rebuilds nothing and states the drift as it stands
        (:func:`~tai42_skeleton.app.lifecycle.config_reload.awaiting_respawn`). A receiver whose
        withdraw took away a value locating its bus never reaches here: the withdraw raises, the
        op fails with the refusal's text, and the process exits.
        """
        if not withdrawn:
            return {"withdrawn": [], "awaiting_respawn": awaiting_respawn()}
        result = await self._reload_after_withdraw()
        return {"withdrawn": withdrawn, "awaiting_respawn": result["awaiting_respawn"]}

    def _spawn_join(self, own_reload: dict[str, Any]) -> None:
        """Start this worker's join (:meth:`_join`) as a task, tracked until it ends and cancelled at shutdown."""
        task = asyncio.create_task(self._join(dict(own_reload)), name="tai-deployment-keys-join")
        self._deployment_keys_join_tasks.add(task)
        task.add_done_callback(self._deployment_keys_join_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    async def _join(self, own_reload: dict[str, Any]) -> None:
        """Announce this worker's deployment keys, then restart the workers that need it, this worker last.

        ``own_reload`` is this worker's own resync reload result, its row in the report the
        recycle decision reads (:meth:`_recycle_after_join`). The join is this worker's own change:
        it runs under one writer-exit scope that holds the worker's exit from the announcement on
        (:func:`~tai42_skeleton.app.writer_exit.hold_writer_exit`), so a recycle op from another
        worker's roll leaves the worker to the join, and settles once the roll ended. The recycle
        targets are pinned before the announcement (:meth:`_pin_join_targets`), as every change pins
        them before its reload: a worker's reply is tied to the life pinned before it answered,
        even when that life leaves before the roll.
        """
        bus = self.bus
        change = f"the deployment keys of {bus.identity.name}"
        with writer_exit_scope() as exits:
            hold_writer_exit(bus, change)
            pinned = await self._pin_join_targets()
            announced = await self._announce_deployment_keys()
            await self._recycle_after_join(announced, own_reload, pinned, change=change)
            await exits.settle(change)

    async def _pin_join_targets(self) -> dict[str, tuple[WorkerKind, int]]:
        """The join's recycle targets, pinned from its op-start census read, before the announcement.

        A census that cannot be read pins nothing (:func:`op_start_census`) and the join goes on
        to announce: a worker whose reply reports keys then answered from a life the join did not
        pin, so the decision asks it once it is ready rather than restarting it at once; when the
        announcement fails too, the roll's own census names whom to ask
        (:meth:`_announce_deployment_keys`).
        """
        from tai42_skeleton.config.recycle_roll import pinned_recycle_targets
        from tai42_skeleton.operations._broadcast import op_start_census

        bus = self.bus
        rows = await op_start_census(bus.census, "deployment_keys_changed", without="no recycle target is pinned")
        if rows is None:
            return {}
        return pinned_recycle_targets(rows, bus.identity.name)

    async def _announce_deployment_keys(self) -> FleetResult:
        """Tell every other worker this one joined, so each withdraws the applied keys its names make owned.

        The announcement carries no names: each receiver re-reads the census, where this
        worker's presence row publishes them, and replies with every recycle-class key it still
        runs on off its effective value (``awaiting_respawn``), as every reload replies. Returns
        the announcement's report. A
        failed publish is logged at ERROR and returns the bus-unreachable shape: no other worker
        stated its env after the announcement, so the roll runs and asks each worker its census
        shows once it is ready, pinned or not
        (:class:`~tai42_skeleton.config.recycle_policy.RecycleDecision` ``report_unreachable``).
        """
        from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome

        bus = self._bus
        if bus is None:
            raise RuntimeError("the deployment keys announcement ran before the bus was built")
        try:
            return await bus.publish(
                DeploymentKeysChanged(op="deployment_keys_changed"),
                targets=None,
                local=LocalApplyResult(outcome=OpOutcome.applied),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "worker bus: announcing this worker's deployment keys to the fleet failed — no other worker stated its "
                "env after the announcement, so each one's state is unknown and the join's roll asks each worker its "
                "census shows once it is ready",
                exc_info=True,
            )
            return FleetResult(op="deployment_keys_changed", reachable=False, error=f"{type(exc).__name__}: {exc}")

    async def _recycle_after_join(
        self,
        announced: FleetResult,
        own_reload: dict[str, Any],
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        change: str,
    ) -> None:
        """Decide from the announcement's replies which workers need a restart, roll over them, this worker last.

        ``pinned`` are the recycle targets read before the announcement (:meth:`_pin_join_targets`). The
        report the decision reads is the announcement's, with this worker's row always its own resync
        reload result ``own_reload`` — this worker's own known statement, which replaces the self
        entry the announcement's publish put in a reachable report and is counted even when the
        announcement could not reach the bus
        (:func:`~tai42_skeleton.config.recycle_policy.with_own_statement`), so this worker is never
        unknown here. A worker that reported keys is restarted, one whose reload was refused at its
        epoch build yet reports keys is left running with its restart withheld, and a sibling whose
        state is unknown is asked once it is ready, by the one decide-and-roll path every
        reload settles through (:func:`~tai42_skeleton.config.recycle_roll.decide_and_roll`). When
        this worker needs its own restart and the roll converged, the roll owes its exit into the
        join's writer-exit scope, which the join settles once the roll ended
        (:mod:`~tai42_skeleton.app.writer_exit`: marked recycling, one graceful exit) for
        its supervisor to start it again. ``change`` names the join in the logs.
        """
        from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
        from tai42_skeleton.config.recycle_policy import with_own_statement
        from tai42_skeleton.config.recycle_roll import decide_and_roll

        bus = self.bus
        identity = bus.identity
        own_row = LocalApplyResult(outcome=OpOutcome.applied, payload=own_reload).as_row(identity.name)
        report = with_own_statement(announced, own_row)
        roll = await decide_and_roll(bus, report, identity, pinned, change=change, orchestrate=orchestrate_recycle)
        if roll.self_exit_armed:
            logger.info("worker bus: %s need this worker's own restart — recycling itself", change)

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
        """Cancel the bus subscription, then the join, the carried rolls and the deferred recycle exit it started.

        Awaits their end.

        The shutdown counterpart of ``_spawn_bus_subscription``. The subscription is the one
        producer of the other three: its resync spawns the join and its op handlers spawn the
        carried rolls and the deferred recycle exit, so it is cancelled and awaited first and
        nothing it would start can begin after the rest are cancelled. The subscription is a
        run-until-cancelled lifespan task, cancelled as every such task is
        (:meth:`_cancel_perpetual_task`); a death of one of the other three was already logged at
        ERROR by its done-callback, so it is awaited here and not raised a second time.
        """
        task, self._bus_subscription_task = self._bus_subscription_task, None
        await self._cancel_perpetual_task(task)
        for join in list(self._deployment_keys_join_tasks):
            join.cancel()
        await self._cancel_carried_rolls()
        deferred_recycle, self._deferred_recycle_task = self._deferred_recycle_task, None
        if deferred_recycle is not None:
            deferred_recycle.cancel()
            # A cancelled exit ends silently; one that raised was logged by its done-callback.
            await asyncio.gather(deferred_recycle, return_exceptions=True)
        # A cancelled join ends silently; a join that raised was logged by its done-callback.
        await asyncio.gather(*self._deployment_keys_join_tasks, return_exceptions=True)

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
        to the local admin primitive. The reload, MCP reload and deregister ops are awaited
        through the reload gate, identically to the HTTP path; the tool reload/remove ops
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
            case Recycle() | CarryRoll() | ReleaseRoll():
                return await self._apply_restart_op(op)
            case DeploymentKeysChanged():
                return await self._converge_after_withdraw(await self._withdraw_fleet_owned_env())
            case _:
                assert_never(op)

    async def _apply_restart_op(self, op: Recycle | CarryRoll | ReleaseRoll) -> dict[str, Any]:
        """Apply a fleet-restart op here: restart for another worker's roll, or hold or start a roll handed here.

        A roll handed over the bus comes from a run in another process, which can end with no
        word, so the held roll starts by that run's bound (``answers_within``) at the latest.
        """
        match op:
            case Recycle():
                return await self._apply_recycle()
            case CarryRoll():
                return await self.accept_carried_roll(op, answers_within=op.answers_within)
            case ReleaseRoll():
                return self._release_carried_roll(op.roll, op.change)
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

        The worker decides to leave in one step with the check that nothing holds its exit
        (:meth:`~tai42_skeleton.app.bus.WorkerBus.decide_departure`), so a carried roll or any
        other change reaching it after the decision refuses to start. A worker already leaving
        writes nothing and arms nothing, and answers ``{"recycling": kind, "already_leaving": reason}``.
        A worker whose exit is deferred to its own in-flight change
        (:attr:`~tai42_skeleton.app.bus.WorkerBus.exit_deferred`) writes nothing and arms nothing
        either, and answers ``{"recycling": kind, "exit_deferred": change}``: that change's reply
        is answered first, and once no change holds the exit any more the worker leaves for this
        op unless it is already leaving (:meth:`_leave_for_deferred_recycle`), so the recycle is
        carried out whether or not the change owed the worker's own restart. Otherwise it writes
        ``state=recycling`` into presence, arms the bus's single-shot post-terminal-reply slot with
        the departure's signal, and answers ``{"recycling": kind}``.

        The decision starts the recycling announce (it begins the life's leaving), and this waits
        for it BEFORE arming the signal — the slot's action is sync and a ``create_task`` there
        would race the SIGTERM — so the census shows WHY this worker is departing; an announce
        that fails sends the signal and raises, since the decision is final
        (:meth:`~tai42_skeleton.app.bus.WorkerBus.announce_departure`). The
        signal is sent only once the terminal reply was sent, so the orchestrator records the
        recycle's outcome before this process departs; a reply that fails to ship still sends it,
        and so does a post-apply handler that fails the op after the decision, its failure
        reported in that reply. The payload names the graceful-exit kind and reason only — never an env
        value.
        """
        bus = self.bus
        kind = bus.identity.kind.value
        refusal = bus.decide_departure(_RECYCLE_OP_REASON)
        if refusal is not None and refusal.kind == "already_leaving":
            logger.info("worker bus: recycle op on a worker already leaving (%s) — nothing armed", refusal.reason)
        elif refusal is not None:
            logger.info(
                "worker bus: recycle op on a worker whose exit is deferred to its own change (%s) — nothing armed",
                refusal.reason,
            )
            self._leave_for_deferred_recycle()
        else:
            await bus.announce_departure()
            bus.arm_post_reply(bus.signal_departure)
        return recycle_reply(kind, refusal)

    def _leave_for_deferred_recycle(self) -> None:
        """Leave for a recycle op deferred to this worker's own changes once they are all answered.

        One task carries every deferred recycle op out
        (:func:`~tai42_skeleton.app.graceful_exit.leave_when_settled`): a second op deferred while
        it waits adds nothing. The task is tracked until it ends, cancelled at shutdown, and a
        failed exit is logged at ERROR by its done-callback.
        """
        from tai42_skeleton.app.graceful_exit import leave_when_settled

        if self._deferred_recycle_task is not None and not self._deferred_recycle_task.done():
            return
        task = asyncio.create_task(leave_when_settled(self.bus, _RECYCLE_OP_REASON), name="tai-deferred-recycle-exit")
        self._deferred_recycle_task = task
        task.add_done_callback(self._log_task_exception)


def _failed_op_statement(op: dict[str, Any], failure: Exception) -> dict[str, Any] | None:
    """The payload of the ``failed`` reply to the delivered ``op`` that failed with ``failure``.

    A ``reload_config`` refused at its epoch build states the discarded build
    (:func:`~tai42_skeleton.app.discarded_build.discarded_build_statement`): the reload changes
    no env before its build, so this worker keeps serving its previous epoch under the env the
    discard restored, and the statement carries the recycle-class drift as it stands after the
    discard, which the recycle decision reads like an applied reply's. Every other failure states
    nothing, a ``deployment_keys_changed`` whose rebuild is refused among them: its withdraw
    changed the env before the rebuild, so the previous epoch keeps serving on values the env no
    longer holds.
    """
    if op.get("op") != "reload_config":
        return None
    return discarded_build_statement(failure)
