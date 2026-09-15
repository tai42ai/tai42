"""The worker-bus subscription lifecycle and fleet-op dispatch."""

import asyncio
import logging
from typing import Any

from tai42_contract.app import tai42_app

from tai42_skeleton.app.bus import WorkerBus, WorkerKind
from tai42_skeleton.app.bus_settings import bus_settings
from tai42_skeleton.app.graceful_exit import graceful_exit_for
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.readiness_sentinel import write_ready_sentinel
from tai42_skeleton.app.reload_gate import reload_gate

logger = logging.getLogger(__name__)


def _op_field(op: dict[str, Any], key: str) -> Any:
    """Return a required field of a delivered fleet op, raising loudly when absent.

    A malformed fleet op must fail its confirmation, never apply a partial op.
    """
    value = op.get(key)
    if value is None:
        raise ValueError(f"{op.get('op')} fleet op missing {key!r}")
    return value


class BusSubscriptionMixin(LifecycleState):
    """Lifecycle mixin owning the worker-bus subscription and fleet-op dispatch."""

    @property
    def bus(self) -> WorkerBus:
        """This process's worker bus, built in ``app_context``.

        The runtime-op publishers and the fleet census route reach the fleet through it. Raises if accessed
        before ``app_context`` builds it.
        """
        bus = self._bus
        if bus is None:
            raise RuntimeError("the worker bus is not built — enter app_context first")
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
            name="tai-worker-bus-subscription",
        )
        self._bus_subscription_task.add_done_callback(self._on_perpetual_task_done)

    async def _resync_on_ready(self) -> None:
        """Self-resync run after subscribe, before presence-register, on every (re)connect.

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
        """
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
        """Cancel the bus subscription and await its termination.

        The shutdown counterpart of ``_spawn_bus_subscription``. A task that
        died with a non-``CancelledError`` exception was already surfaced at
        ERROR by its done-callback, so it is awaited-and-swallowed here rather
        than re-raised — this runs inside ``app_context``'s shutdown
        ``finally``, and one dead task must not skip the remaining teardown.
        """
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

    async def _apply_bus_op(self, op: dict[str, Any]) -> Any:
        """Apply one fleet op delivered from a sibling worker (or the self-resync), then fire the post-apply hooks.

        The sibling-worker counterpart of a route-received admin call: it maps each op
        to the local admin primitive. The heavy sync ops run on a worker thread through
        the reload gate, identically to the HTTP path; the tool reload/remove ops
        dispatch through the async per-kind reloader; ``list_failed_mcps`` is a plain
        in-process read. An unrecognized op raises loudly.

        After the op applies, ``on_fleet_op_applied`` handlers fire with the op name —
        the op has not fully "applied" until they finish (a celery worker must re-fork
        its prefork pool before it reports applied), and a raising handler fails the op.
        The returned value becomes the op's terminal ``applied`` payload; the publisher
        echo-skips its own broadcast, so this never re-applies a self-op.
        """
        result = await self._dispatch_bus_op(op)
        await self._run_fleet_op_applied_handlers(_op_field(op, "op"))
        return result

    async def _dispatch_bus_op(self, op: dict[str, Any]) -> Any:
        """Route one fleet op to its local admin primitive and return its result."""
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
        if op_name in ("evict_template", "clear_template_cache"):
            return self._apply_template_op(op)
        if op_name == "recycle":
            return await self._apply_recycle()
        raise ValueError(f"unknown fleet op {op_name!r}")

    def _apply_template_op(self, op: dict[str, Any]) -> dict[str, Any]:
        """Apply a template-cache fleet op on this worker.

        Evict one rendered template (or a whole directory under a key) for ``evict_template``, else clear
        the entire compiled-template cache for ``clear_template_cache``.
        """
        if op.get("op") == "evict_template":
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
        tai42_app.storage.resource_manager.clear_cache()
        return {"cleared": True}

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
        kind = self.bus.identity.kind
        await self.bus.mark_recycling()
        self.bus.arm_post_reply(graceful_exit_for(kind))
        return {"recycling": kind.value}
