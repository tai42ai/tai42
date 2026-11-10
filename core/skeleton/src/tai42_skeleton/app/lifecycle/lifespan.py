"""The worker lifespan context manager, resource teardown, and the reaper task spawn/cancel."""

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from tai42_kit.compute import stop_compute_pool

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.boot_rules import (
    require_apply_window_covers_drain,
    require_bus_for_backend,
    require_bus_for_shared_config,
    require_bus_for_workers,
    require_hooks_store_for_backend,
    require_hooks_store_for_workers,
    require_sub_mcp_store_for_workers,
)
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.readiness_sentinel import remove_ready_sentinel
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.settings.owned_settings import require_known_owned_settings
from tai42_skeleton.tools import mcp_start
from tai42_skeleton.utils.tasks import cancel_and_wait

logger = logging.getLogger(__name__)


def _require_deployment_invariants(manifest: Manifest) -> None:
    """Refuse loudly, before the app starts, a deployment whose shared state the processes cannot agree on.

    Checked at the one seam both `tai serve` and `tai backend` cross.
    """
    # A process with a registered backend, or a shared-config deployment, must have the
    # worker bus configured — otherwise sibling workers or instances serve stale config
    # after a reload.
    require_bus_for_shared_config()
    require_bus_for_backend(manifest)
    # A registered backend runs in its own process, which matches the events it raises
    # against its own hooks registry, so that registry must be shared.
    require_hooks_store_for_backend(manifest)
    # A server running more than one worker needs the bus, the sub-MCP registration store
    # and the hooks registry shared, read under the stored env the boot env bridge applied.
    require_bus_for_workers()
    require_sub_mcp_store_for_workers()
    require_hooks_store_for_workers()
    # A bus-enabled process's fleet apply window must cover the retire's drain floor, so
    # a worker draining real work answers inside it.
    require_apply_window_covers_drain()


class _StopSteps:
    """The steps of one stop, run in order, each one whatever an earlier one raised.

    The stop owns what its log says about its end. When every step ran with no failure it
    logs that at INFO. When steps failed, :meth:`run` raises what they raised once the last
    step ran: one failure as itself, several together as an ``ExceptionGroup``. A cancellation
    (or any other ``BaseException``) is no step failure: it propagates at once, after the
    failures collected before it are logged at ERROR and a WARNING names the step it cut short
    and every step that never ran, so cutting the stop short loses none of them.
    """

    def __init__(self, stop: str, steps: Sequence[tuple[str, Callable[[], object]]]) -> None:
        self._stop = stop
        self._steps = tuple(steps)

    async def run(self) -> None:
        """Run every step (sync or async) in order, then raise what the steps raised."""
        failures: list[Exception] = []
        for index, (label, step) in enumerate(self._steps):
            try:
                result = step()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                failures.append(e)
            except BaseException as cut:
                for failure in failures:
                    logger.exception(
                        "%s cut short during %s; an earlier step failed", self._stop, label, exc_info=failure
                    )
                never_ran = ", ".join(later for later, _ in self._steps[index + 1 :]) or "none"
                logger.warning(
                    "%s cut short by %s during %s, which did not finish; never ran: %s",
                    self._stop,
                    type(cut).__name__,
                    label,
                    never_ran,
                )
                raise
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup(f"{self._stop} failed", failures)
        logger.info("%s finished: every step ran", self._stop)


class LifespanMixin(LifecycleState):
    """Process startup/shutdown hooks and the long-lived background reapers mixed into the app."""

    @asynccontextmanager
    async def app_context(self, manifest: Manifest):
        """The process lifespan for ``manifest`` — start background tasks on enter, tear everything down on exit.

        Joins the worker bus the boot manifest read built (``read_boot_manifest``), so that read
        comes first: entering without it raises.
        """
        if self._bus is None:
            raise RuntimeError("read_boot_manifest builds the worker bus; call it first")
        _require_deployment_invariants(manifest)
        from tai42_skeleton.app.epoch import install_boot_core

        try:
            # Install boot epoch 0's serving core BEFORE start(), so start() and the
            # epoch handlers register into this generation's fresh FastMCP and every
            # process-spine read resolves to it. Built FRESH here (via the builder)
            # under the started env rather than reusing the construction-time scaffold,
            # so a sequential app_context in the same process never inherits a prior
            # generation's collaborators. Dropping the scaffold routes reads through the
            # installed epoch; the worker lifespan attaches the dispatch slot + serving
            # app (an embedded / pure-``app_context`` caller needs none).
            install_boot_core(self._build_serving_core())
            self._scaffold_core = None
            await self.start(manifest)
            # Every plugin is imported now, so every settings group is registered: an env
            # name the process environment or the ``.env`` file sets under an owned prefix
            # that no group accepts refuses the boot (``start()`` audited the applied env).
            require_known_owned_settings()
            # raise_on_error: a failed startup handler must abort the boot
            # loudly, never leave a healthy-looking half-initialized app.
            await self._run_handlers(list(self._startup_handlers.values()), raise_on_error=True)
            # Bind this loop as the serving loop: a reload body driven from another loop (an
            # in-process job's own loop) runs here (``reload_gate.run``).
            self.bind_serving_loop()
            # The connectors' credential refreshes run as tasks owned by this loop, so a
            # caller that stops waiting (a budget, a cancelled request) never cuts one.
            refresh_owner.bind_to_running_loop()
            # Boot listed before the owner was live, so a server that demands a credential whose
            # token expired meanwhile is still failed: refresh such credentials now and re-probe
            # those entries once, before the post-swap handlers and the background tasks start.
            await self._bring_boot_credentials_up_to_date()
            # Establish the boot generation's loop-affine background loops ON this
            # serving loop, registered with the boot epoch so they retire with it. Boot
            # raises loudly on a failed establisher (never a healthy-looking half-start).
            # A reload re-establishes them post-swap through the build+swap primitive.
            await self._run_post_swap_handlers(raise_on_error=True)
            # Join the worker bus: open the single long-lived subscription on the one bus
            # the boot manifest read built. The subscription registers presence and
            # self-resyncs through on_ready on every (re)connect.
            self._spawn_bus_subscription()
            # Start the failed-MCP re-probe task so a server that failed its boot
            # probe self-heals on an exponential backoff without a manual reload.
            self._spawn_reprobe_task()
            # Start the async-park expiry reaper so a parked ask's continuation
            # fires once its expiry passes even with no blocking waiter to trip it.
            self._spawn_interactions_reaper()
            # Start the sandbox session reap loop, but only when a provider backs the
            # slot — the reaper is a no-op door otherwise, so it is never spawned
            # without a provider to reap through.
            self._spawn_sandbox_reaper()
            # Start the inbound-media retention reaper so an abandoned pending upload and
            # an expired bound blob are reclaimed on a fixed cadence. Spawned
            # unconditionally (a no-op each pass absent a store/provider), so both the
            # serve and backend processes reap.
            self._spawn_media_reaper()
            # Start the monitoring health watch so a growth of the run family's monitoring
            # delivery-failure counters raises its platform event within one poll.
            self._spawn_monitoring_health_watch()
            yield self
        finally:
            # Shutdown start: drop the readiness sentinel FIRST so the readiness probe
            # fails immediately and this process leaves the load balancer's endpoints
            # before draining begins.
            remove_ready_sentinel()
            # Begin this worker's leaving, whatever asked it to stop, and wait for its census
            # row to read ``recycling`` (with the declared exit drain) before it stops taking
            # part in ops. A leaving already begun — a departure decision, or the first stop
            # signal the host observed — keeps its first reason and its first announce. The row
            # stays, fresh, through every step of the stop until ``leave_census``.
            self.bus.stop_began("shutdown")
            try:
                await self._stop_serving()
            finally:
                # Every step of the stop ran on the serving loop; from here the process serves from none.
                self.unbind_serving_loop()

    async def _stop_serving(self) -> None:
        """Run every step of this process's stop, in order, then raise what the steps raised.

        Each step runs whatever an earlier one raised, and the stop logs how it ended: finished,
        or cut short by a cancellation with the steps that never ran (:class:`_StopSteps`). The
        wait for the leaving's announce comes first and op participation ends next; the census life
        (heartbeat, claim, row) outlives every step until ``leave_census``, after the shutdown
        handlers, and the resources and the serving generation are torn down last.
        """
        from tai42_skeleton.app.epoch import clear_epoch

        await _StopSteps(
            "shutdown stop",
            (
                ("leaving announce", self.bus.leaving_announced),
                # End op participation only: the census life (heartbeat, claim, row) outlives it.
                ("bus subscription cancel", self._cancel_bus_subscription),
                ("re-probe task cancel", self._cancel_reprobe_task),
                # Cancel every MCP server start still running on the serving loop.
                ("MCP starts cancel", mcp_start.aclose),
                ("interactions reaper cancel", self._cancel_interactions_reaper),
                ("sandbox reaper cancel", self._cancel_sandbox_reaper),
                ("media reaper cancel", self._cancel_media_reaper),
                ("monitoring health watch cancel", self._cancel_monitoring_health_watch),
                # Stop the hook-run sweep and give the running hook-run attempts their grace
                # while credential refreshes and pooled clients are still live, before the
                # shutdown handlers drain the tool supervisors.
                ("hook runs stop", self._stop_hook_runs),
                # Cancel the owned credential refreshes; a refresh mid write-back finishes first.
                ("credential refreshes close", refresh_owner.aclose),
                # Shutdown keeps swallow-and-log so teardown runs every handler.
                ("shutdown handlers", lambda: self._run_handlers(list(self._shutdown_handlers.values()))),
                # End this process life's census record once the last declared shutdown wait is
                # over: the announced drain covers exactly the steps the row lived through.
                ("census leave", self.bus.leave_census),
                ("resource teardown", self._teardown_resources),
                # Drop the process serving generation (closing its FastMCP lifespan), then
                # leave a fresh boot-scaffold core as the read target of the un-booted app, as
                # construction does: a post-context read sees none of the dropped generation's
                # state, and a later app_context in this process starts from a clean slate.
                ("serving generation drop", clear_epoch),
                ("boot scaffold rebuild", self._rebuild_boot_scaffold),
            ),
        ).run()

    def _rebuild_boot_scaffold(self) -> None:
        """Leave a fresh boot-scaffold core as the un-booted app's read target."""
        self._scaffold_core = self._build_serving_core()

    @staticmethod
    def _log_task_exception(task: asyncio.Task[Any]) -> bool:
        """Log a lifespan-owned background task's terminal exception at ERROR.

        Cancellation (shutdown) is the normal stop and stays silent;
        any other exception means the task stopped doing its job, so log it at
        ERROR with the task's name. Returns True when the task instead returned a
        value cleanly (no cancellation, no exception), letting a caller treat an
        unexpected clean return as its own failure.
        """
        if task.cancelled():
            return False
        exc = task.exception()
        if exc is not None:
            logger.error("background task %r terminated with an exception", task.get_name(), exc_info=exc)
            return False
        return True

    def _on_perpetual_task_done(self, task: asyncio.Task[Any]) -> None:
        """Done-callback shared by all six run-until-cancelled lifespan tasks.

        The worker-bus subscription, the failed-MCP re-probe loop, the
        interactions expiry reaper, the sandbox reaper, the media retention
        reaper and the monitoring health watch, in both the serve and the backend
        process, register this. A clean
        cancellation (shutdown) stays
        silent and does nothing. Otherwise the perpetual task stopped doing its
        job — a runtime exception OR a NORMAL return, both a death, since these
        tasks are contractually perpetual — so the death is logged at ERROR, the
        marker ``_dead_perpetual_task`` is set (``/ready`` then names the task and
        503s), and this process requests its own graceful exit through its bus's exit latch
        (:meth:`~tai42_skeleton.app.bus.WorkerBus.request_graceful_exit`, one signal per life)
        so a ``restart: unless-stopped`` supervisor reboots it from a clean baseline.
        """
        if task.cancelled():
            return
        if self._log_task_exception(task):
            logger.error(
                "perpetual background task %r returned unexpectedly; it must run until cancelled",
                task.get_name(),
            )
            reason = "returned"
        else:
            reason = type(task.exception()).__name__
        self._dead_perpetual_task = (task.get_name(), reason)
        bus = self._bus
        if bus is None:
            raise RuntimeError(f"perpetual task {task.get_name()!r} died with no worker bus built to request the exit")
        bus.request_graceful_exit(f"perpetual task {task.get_name()} {reason}")

    @staticmethod
    async def _cancel_perpetual_task(task: asyncio.Task[Any] | None) -> None:
        """Cancel a run-until-cancelled lifespan task at shutdown and await its end; ``None`` is a no-op.

        The shutdown counterpart of :meth:`_on_perpetual_task_done`, which every such task
        registers: a non-``CancelledError`` death was already surfaced at ERROR there, which also
        requested this process's graceful exit, so it is awaited here and not raised a second
        time. A cancellation of the stop itself while it waits propagates (:func:`cancel_and_wait`).
        """
        if task is None:
            return
        with contextlib.suppress(Exception):
            await cancel_and_wait(task)

    async def _teardown_resources(self) -> None:
        """Release process-wide resources at shutdown.

        Runs on ``app_context`` — the single seam both the served (HTTP / stdio)
        and backend-worker entrypoints cross — so neither path leaks the kit's
        loop-bound resources (pooled clients, checkpoint savers, stores) or the
        compute pool's worker processes, nor loses buffered monitoring spans. Each
        step runs whatever an earlier one raised (:class:`_StopSteps`), and what the
        steps raised is raised once the last ran.
        """
        await _StopSteps(
            "shutdown teardown",
            (
                # Releases only: ``app_context`` does not own the loop, so it does not mark it
                # closing; a later ``app_context`` on the same loop leases anew.
                ("loop-bound kit resources", lambda: _lifecycle.release_loop_bound_resources()),
                # End the process's compute pool and its worker processes; a job still running
                # raises in its caller. The next compute job in this process starts a fresh pool.
                ("compute pool stop", lambda: asyncio.to_thread(stop_compute_pool)),
                # Flush buffered monitoring spans last, so spans emitted during the
                # teardown above are captured before the process exits rather than lost
                # to a SIGTERM / container restart ahead of the SDK's periodic flush.
                ("monitoring flush", lambda: _lifecycle.get_monitoring().writer.flush()),
            ),
        ).run()

    def _spawn_interactions_reaper(self) -> None:
        """Start the async-park expiry reaper loop on the serving loop.

        Owned by ``app_context``; runs until cancelled at shutdown. A no-op each pass when the
        interactions store is unconfigured.
        """
        from tai42_skeleton.interactions.reaper import run_expiry_reaper_loop

        self._interactions_reaper_task = asyncio.create_task(
            run_expiry_reaper_loop(),
            name="tai-interactions-expiry-reaper",
        )
        # Backstop a silent death OR an unexpected normal return loudly, mirroring the
        # worker-bus subscription and re-probe loop: a run-until-cancelled task.
        self._interactions_reaper_task.add_done_callback(self._on_perpetual_task_done)

    async def _cancel_interactions_reaper(self) -> None:
        """Cancel the expiry reaper and await its termination at shutdown.

        The shutdown counterpart of ``_spawn_interactions_reaper`` (:meth:`_cancel_perpetual_task`).
        """
        task, self._interactions_reaper_task = self._interactions_reaper_task, None
        await self._cancel_perpetual_task(task)

    def _spawn_sandbox_reaper(self) -> None:
        """Start the sandbox session reap loop on the serving loop, but ONLY when a provider is registered.

        The loop is never started absent one. Owned by ``app_context``; runs until cancelled at
        shutdown.
        """
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
        """Cancel the sandbox reap loop and await its termination at shutdown.

        The shutdown counterpart of ``_spawn_sandbox_reaper`` (:meth:`_cancel_perpetual_task`).
        """
        task, self._sandbox_reaper_task = self._sandbox_reaper_task, None
        await self._cancel_perpetual_task(task)

    def _spawn_media_reaper(self) -> None:
        """Start the inbound-media retention reaper loop on the serving loop.

        Owned by ``app_context``; runs until cancelled at shutdown. A no-op each pass when the
        conversations store is unconfigured or no blob provider is registered.
        """
        from tai42_skeleton.conversations.media_reaper import run_media_reaper_loop

        self._media_reaper_task = asyncio.create_task(
            run_media_reaper_loop(),
            name="tai-media-retention-reaper",
        )
        # Backstop a silent death OR an unexpected normal return loudly, mirroring the
        # interactions expiry reaper: a run-until-cancelled task.
        self._media_reaper_task.add_done_callback(self._on_perpetual_task_done)

    async def _cancel_media_reaper(self) -> None:
        """Cancel the media retention reaper and await its termination at shutdown.

        The shutdown counterpart of ``_spawn_media_reaper`` (:meth:`_cancel_perpetual_task`).
        """
        task, self._media_reaper_task = self._media_reaper_task, None
        await self._cancel_perpetual_task(task)

    def _spawn_monitoring_health_watch(self) -> None:
        """Start the monitoring health watch loop on the serving loop.

        Owned by ``app_context``; runs until cancelled at shutdown. A failed pass is logged
        and the next one runs, so only a defect in the loop itself ends it.
        """
        from tai42_skeleton.monitoring.health_watch import run_monitoring_health_watch

        self._monitoring_health_watch_task = asyncio.create_task(
            run_monitoring_health_watch(),
            name="tai-monitoring-health-watch",
        )
        self._monitoring_health_watch_task.add_done_callback(self._on_perpetual_task_done)

    @staticmethod
    async def _stop_hook_runs() -> None:
        """Stop the hook-run sweep and settle every hook-run attempt of this process at shutdown.

        A failure is logged with its stack trace and does not fail the stop: an attempt left
        unsettled is reclaimed once its lease lapses.
        """
        from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep

        try:
            await stop_hook_runs_sweep()
        except Exception:
            logger.exception(
                "hook runs: the shutdown stop failed; unsettled runs are reclaimed when their lease lapses"
            )

    async def _cancel_monitoring_health_watch(self) -> None:
        """Cancel the monitoring health watch and await its termination at shutdown.

        The shutdown counterpart of ``_spawn_monitoring_health_watch`` (:meth:`_cancel_perpetual_task`).
        """
        task, self._monitoring_health_watch_task = self._monitoring_health_watch_task, None
        await self._cancel_perpetual_task(task)
