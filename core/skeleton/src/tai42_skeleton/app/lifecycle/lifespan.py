"""The worker lifespan context manager, resource teardown, and the interactions/sandbox reaper task spawn/cancel."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.boot_rules import require_bus_for_backend, require_bus_for_shared_config
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.readiness_sentinel import remove_ready_sentinel
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.manifest import Manifest

logger = logging.getLogger(__name__)


class LifespanMixin(LifecycleState):
    """Process startup/shutdown hooks and the long-lived background reapers mixed into the app."""

    @asynccontextmanager
    async def app_context(self, manifest: Manifest, *, kind: WorkerKind = WorkerKind.serve):
        """The process lifespan for ``manifest`` — start background tasks on enter, tear everything down on exit."""
        # Bus/boot invariant at the one seam both `tai serve` and `tai backend`
        # cross: a process with a registered backend, or a shared-config deployment,
        # must have the worker bus configured — otherwise sibling workers or instances
        # serve stale config after a reload. Refuse loudly before the app starts.
        require_bus_for_shared_config()
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
            # Start the async-park expiry reaper so a parked ask's continuation
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

    @classmethod
    def _on_perpetual_task_done(cls, task: asyncio.Task[Any]) -> None:
        """Done-callback for a run-until-cancelled lifespan-owned task.

        Covers the worker-bus subscription and the failed-MCP re-probe loop.
        A clean cancellation stays silent and a runtime exception is logged at
        ERROR — and, unlike a bounded task, a NORMAL return is ALSO logged at
        ERROR: these tasks are contractually perpetual, so returning means the
        worker silently stopped doing its job (a subscription that returns stops
        receiving sibling reloads; the re-probe loop stops self-healing).
        """
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
        await _guard("checkpoint registry", lambda: _lifecycle.checkpoint_registry().close_all())
        await _guard("store registry", lambda: _lifecycle.store_registry().close_all())

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
