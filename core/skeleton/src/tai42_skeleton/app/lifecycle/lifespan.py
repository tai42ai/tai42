"""The worker lifespan context manager, resource teardown, and the reaper task spawn/cancel."""

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

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
from tai42_skeleton.app.graceful_exit import request_graceful_exit
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.readiness_sentinel import remove_ready_sentinel
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.settings.owned_settings import require_known_owned_settings

logger = logging.getLogger(__name__)


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
        # Bus/boot invariant at the one seam both `tai serve` and `tai backend`
        # cross: a process with a registered backend, or a shared-config deployment,
        # must have the worker bus configured — otherwise sibling workers or instances
        # serve stale config after a reload. Refuse loudly before the app starts.
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
            # Every plugin is imported now, so every settings group is registered: an env
            # name the process environment or the ``.env`` file sets under an owned prefix
            # that no group accepts refuses the boot (``start()`` audited the applied env).
            require_known_owned_settings()
            # raise_on_error: a failed startup handler must abort the boot
            # loudly, never leave a healthy-looking half-initialized app.
            await self._run_handlers(list(self._startup_handlers.values()), raise_on_error=True)
            # Remember the serving loop, so a reload running on a worker thread can
            # marshal loop-affine work (preset reconcile, checkpoint/store close) back
            # onto the serving loop.
            self._serving_loop = asyncio.get_running_loop()
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
            await self._cancel_bus_subscription()
            await self._cancel_reprobe_task()
            await self._cancel_interactions_reaper()
            await self._cancel_sandbox_reaper()
            await self._cancel_media_reaper()
            await self._cancel_monitoring_health_watch()
            # Stop the hook-run sweep and give the running hook-run attempts their grace
            # while credential refreshes and pooled clients are still live, before the
            # shutdown handlers drain the tool supervisors.
            await self._stop_hook_runs()
            # Cancel the owned credential refreshes; a refresh mid write-back finishes first.
            await refresh_owner.aclose()
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
        503s), and this process requests its own graceful exit so a
        ``restart: unless-stopped`` supervisor reboots it from a clean baseline.
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
        request_graceful_exit(os.getpid())

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
        await _guard("loop-bound kit resources", lambda: _lifecycle.release_loop_bound_resources(all_epochs=True))

        # Flush buffered monitoring spans last, so spans emitted during the
        # teardown above are captured before the process exits rather than lost
        # to a SIGTERM / container restart ahead of the SDK's periodic flush.
        try:
            _lifecycle.get_monitoring().writer.flush()
        except Exception as e:
            logger.error("Error during monitoring flush: %s", e, exc_info=True)
            errors.append(e)

        if errors:
            raise ExceptionGroup("shutdown teardown failed", errors)

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

        The shutdown counterpart of ``_spawn_interactions_reaper``.
        A non-``CancelledError`` death was already surfaced at ERROR by the
        done-callback, so it is awaited-and-swallowed here (this runs inside
        ``app_context``'s shutdown ``finally``, where re-raising would skip the
        remaining teardown).
        """
        task = self._interactions_reaper_task
        self._interactions_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 task failure already surfaced by the done-callback; swallowed so shutdown completes
            pass

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

        The shutdown counterpart of ``_spawn_sandbox_reaper``.
        A non-``CancelledError`` death was already surfaced at ERROR by the
        done-callback, so it is awaited-and-swallowed here (this runs inside
        ``app_context``'s shutdown ``finally``, where re-raising would skip the
        remaining teardown).
        """
        task = self._sandbox_reaper_task
        self._sandbox_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 task failure already surfaced by the done-callback; swallowed so shutdown completes
            pass

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

        The shutdown counterpart of ``_spawn_media_reaper``.
        A non-``CancelledError`` death was already surfaced at ERROR by the
        done-callback, so it is awaited-and-swallowed here (this runs inside
        ``app_context``'s shutdown ``finally``, where re-raising would skip the
        remaining teardown).
        """
        task = self._media_reaper_task
        self._media_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 task failure already surfaced by the done-callback; swallowed so shutdown completes
            pass

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

        A failure is logged with its stack trace and not re-raised: this runs inside
        ``app_context``'s shutdown ``finally``, where re-raising would skip the remaining
        teardown. An attempt left unsettled is reclaimed once its lease lapses.
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

        The shutdown counterpart of ``_spawn_monitoring_health_watch``. A
        non-``CancelledError`` death was already surfaced at ERROR by the done-callback, so
        it is awaited-and-swallowed here (this runs inside ``app_context``'s shutdown
        ``finally``, where re-raising would skip the remaining teardown).
        """
        task = self._monitoring_health_watch_task
        self._monitoring_health_watch_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 task failure already surfaced by the done-callback; swallowed so shutdown completes
            pass
