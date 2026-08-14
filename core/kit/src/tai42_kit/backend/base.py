"""``ManagedBackend`` — the execution-backend launch lifecycle, written once."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from functools import partial
from typing import ClassVar, Final

from tai42_contract.app import tai42_app
from tai42_contract.backend import Backend
from tai42_contract.backend.runtime import (
    BUS_APPLY_TIMEOUT_DEFAULT,
    BUS_APPLY_TIMEOUT_ENV,
    REGISTRY_MUTATING_FLEET_OPS,
    BackendRuntime,
    ExecutionMode,
)

from tai42_kit.backend.conformance import OVERRIDDEN_LAUNCH_VERDICT, check_runtime_declarations
from tai42_kit.backend.settings import BackendDispatchSettings
from tai42_kit.signals import SignalSubscription, signal_chain

logger = logging.getLogger(__name__)

# Upper bound on the wait for the app to become boot-ready before a runtime
# consumes — a loud backstop for a boot that never completes.
DEFAULT_READY_TIMEOUT_SECONDS: Final[float] = 120.0

# Upper bound on the wait for a run body to return after a drain was requested.
#
# It must sit ABOVE the longest job the engine can be running, because a warm
# drain waits for in-flight work to finish: a bound below the job budget abandons
# — and so severs — exactly the work the drain exists to protect. The default is
# the dispatch surface's own ``task_timeout`` default (300s) plus a margin for the
# engine's own teardown. A binding that knows its live job budget overrides
# ``drain_timeout`` from settings rather than living with this default, and the
# deployment's termination grace must be at least as long or the supervisor's kill
# lands first.
DEFAULT_DRAIN_TIMEOUT_SECONDS: Final[float] = 330.0

# The whole-turnover budget is derived from the bus apply window (the contract's
# host agreement) less a margin, and floored, so a confirm-or-raise lands before
# the publisher's report cut and a stall records a truthful ``failed`` rather
# than a guessed ``timed_out``.
_TURNOVER_BUDGET_MARGIN = 3.0
_TURNOVER_BUDGET_FLOOR = 5.0


class ManagedBackend(Backend):
    """Execution-backend base implementing the whole launch lifecycle once.

    Subcommand dispatch, boot-readiness gating, off-loop running, composed
    warm/cold shutdown wiring, drain-on-cancellation, and capability-conditional
    pool turnover after a registry-mutating fleet op all live here. A vendor
    binding declares :attr:`runtimes` and implements
    :class:`~tai42_contract.backend.runtime.BackendRuntime`; it wires no signal
    handler, awaits no latch, and registers no lifecycle hook of its own.
    """

    label: ClassVar[str]
    """This backend's name in operator-facing messages."""

    runtimes: ClassVar[Mapping[str, type[BackendRuntime]]]
    """Launch subcommand -> the runtime it selects."""

    ready_timeout: ClassVar[float] = DEFAULT_READY_TIMEOUT_SECONDS

    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS
    """How long a drain may take before the wait is abandoned.

    Declared per class like the rest, but deliberately NOT a ``ClassVar``: the
    budget has to cover the engine's LIVE job budget, which is a settings read, so
    a binding overrides this with a property (the same shape as
    :attr:`dispatch_settings`) rather than freezing a number at import."""

    # Whether THIS launch abandoned its drain wait. Set per launch (the process
    # launches once), and read only by :meth:`_release`, which uses it to tell an
    # expected teardown cancellation from a real one.
    _drain_overran: bool = False

    @property
    def dispatch_settings(self) -> BackendDispatchSettings:
        """This backend's env group carrying the host-agreed dispatch surface.

        Required only when a runtime declares ``pool_turnover_required``: the
        replacement workers inherit the env, so the exported manifest is
        refreshed under this group's ``manifest_key`` before the pool turns over.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares a runtime requiring pool turnover but exposes no dispatch_settings, "
            "so the manifest env key the replacement workers read cannot be refreshed"
        )

    async def launch(self, args: Sequence[str]) -> None:
        """Run one launch subcommand to completion. NEVER overridden — the
        template is what makes every backend behave identically around the
        vendor binding."""
        self._drain_overran = False
        runtime = self._select(args)
        if not runtime.consumes_work:
            # A runtime that pulls no work has nothing to gate on and nothing to
            # drain: an ``inline`` one keeps whatever signal disposition its
            # engine installs, which is safe because nothing else runs on its loop.
            # It still BUILDS: constructing engine objects is every runtime's own
            # step, and only the readiness gate and the drain wiring are the
            # consumer's.
            try:
                await runtime.build()
                await self._run_body(runtime)
            except BaseException:
                await self._release(runtime, propagating=True)
                raise
            await self._release(runtime, propagating=False)
            return

        # Registered BEFORE the build, so an op landing during a long boot is not
        # missed. There is no deregistration seam and none is needed: registration
        # is launch-scoped and the process launches once.
        self._install_turnover(runtime)
        try:
            await runtime.build()
            await self._await_ready(runtime)
            with self._drain_wiring(runtime):
                await self._run_body(runtime)
        except BaseException:
            await self._release(runtime, propagating=True)
            raise
        await self._release(runtime, propagating=False)

    # -- dispatch ------------------------------------------------------------

    def _select(self, args: Sequence[str]) -> BackendRuntime:
        """Resolve the subcommand to a validated, constructed runtime."""
        if not args:
            raise ValueError(f"{self.label} launch requires a subcommand: {', '.join(self.runtimes)}")
        subcommand, *rest = args
        runtime_cls = self.runtimes.get(subcommand)
        if runtime_cls is None:
            raise ValueError(
                f"Unknown {self.label} launch subcommand {subcommand!r}; expected one of: {', '.join(self.runtimes)}"
            )
        runtime = runtime_cls.from_args(rest)
        self._validate(runtime)
        return runtime

    def _validate(self, runtime: BackendRuntime) -> None:
        """Refuse a runtime whose declarations and bindings disagree, before
        anything starts.

        Checked on the INSTANCE, not the class: ``pool_turnover_required`` may be
        decided by a launch option, so a runtime that turns the capability on
        must have honored it.
        """
        problems = check_runtime_declarations(runtime)
        if type(self).launch is not ManagedBackend.launch:
            problems.append(OVERRIDDEN_LAUNCH_VERDICT.format(backend=type(self).__name__))
        if problems:
            raise TypeError(f"{self.label} launch refused:\n- " + "\n- ".join(problems))

    # -- readiness -----------------------------------------------------------

    async def _await_ready(self, runtime: BackendRuntime) -> None:
        """Block until the boot self-resync has built the tool registry.

        A runtime consuming before that finished would run work against a
        half-built registry and fail it permanently, so a boot that never
        completes is a refusal rather than a degraded start.
        """
        try:
            await asyncio.wait_for(tai42_app.lifecycle.wait_until_ready(), timeout=self.ready_timeout)
        except TimeoutError as exc:
            raise RuntimeError(
                f"{self.label} {runtime.name}: the app did not become ready within {self.ready_timeout}s "
                "(boot self-resync never completed); refusing to consume queued work against a half-built "
                "tool registry"
            ) from exc

    # -- running -------------------------------------------------------------

    async def _run_body(self, runtime: BackendRuntime) -> None:
        """Drive the run body in the way its mode declares, and — for a CONSUMING
        runtime — drain it if this coroutine is cancelled out from under it.

        A consuming body is awaited through :func:`asyncio.shield` so an outer
        cancellation — which on a recycle arrives on the SAME signal as the drain
        request — unwinds THIS coroutine without severing in-flight work: the
        drain is requested and the body is awaited to completion before the
        cancellation is re-raised.

        A NON-consuming body holds no in-flight work and declares no
        ``request_drain``, so there is nothing to shield: the body is awaited
        directly. What the cancellation then reaches depends on the mode — an
        ``on_loop`` body receives it and unwinds, while a THREAD body cannot be
        cancelled at all: only the bridge is, and the thread runs on. That case is
        reported and the thread is abandoned as a daemon; see
        :meth:`_report_abandoned_body`.
        """
        if runtime.mode is ExecutionMode.inline:
            runtime.run_blocking()
            return

        thread: threading.Thread | None = None
        if runtime.mode is ExecutionMode.on_loop:
            body: asyncio.Future[None] = asyncio.ensure_future(runtime.run_on_loop())
        else:
            body, thread = self._spawn_daemon(runtime)
        if not runtime.consumes_work:
            try:
                await body
            except asyncio.CancelledError:
                self._report_abandoned_body(runtime, thread)
                raise
            return
        try:
            await asyncio.shield(body)
        except asyncio.CancelledError:
            runtime.request_drain()
            self._drain_overran = await self._await_drain(runtime, body)
            raise

    def _report_abandoned_body(self, runtime: BackendRuntime, thread: threading.Thread | None) -> None:
        """Report a run body the cancellation could not stop.

        Cancelling the bridge future ends the AWAIT, never the thread: a thread
        already inside the run body keeps running. Nothing here can stop it, so it
        is abandoned as a daemon and reported at ERROR — the same bounded-and-loud
        discipline as an overrun drain, with the supervisor's kill as the backstop.
        """
        if thread is None or not thread.is_alive():
            return
        logger.error(
            "%s %s: the cancellation cannot stop a run body already on its thread; abandoning it on its "
            "daemon thread so teardown can run",
            self.label,
            runtime.name,
        )

    def _spawn_daemon(self, runtime: BackendRuntime) -> tuple[asyncio.Future[None], threading.Thread]:
        """Run a blocking body on a dedicated DAEMON thread, bridged to the loop.

        Deliberately not ``to_thread``/``run_in_executor``: a wedged engine on a
        default-executor thread hangs ``asyncio.run``'s
        ``shutdown_default_executor`` at interpreter exit, so the process the
        supervisor is trying to stop never exits. A daemon thread cannot.
        """
        bridge: Future[None] = Future()

        def _body() -> None:
            bridge.set_running_or_notify_cancel()
            try:
                runtime.run_blocking()
            except BaseException as exc:
                # Nothing above this frame can see a thread's exception, so it is
                # carried across the bridge and re-raised on the loop instead of
                # dying with the thread.
                bridge.set_exception(exc)
            else:
                bridge.set_result(None)

        thread = threading.Thread(target=_body, name=f"tai-backend-{self.label}-{runtime.name}", daemon=True)
        thread.start()
        # The thread comes back with the future because cancelling the future does
        # NOT stop the thread — the caller has to be able to see that.
        return asyncio.wrap_future(bridge), thread

    async def _await_drain(self, runtime: BackendRuntime, body: asyncio.Future[None]) -> bool:
        """Wait out the drain, BOUNDED and LOUD. True when the wait was abandoned.

        Overrunning is reported at ERROR and then abandoned rather than waited on
        forever: teardown must still run, and the supervisor's kill is the
        backstop for an engine that will not let go. The body is shielded again
        so the timeout abandons the WAIT, never the drain itself.
        """
        try:
            await asyncio.wait_for(asyncio.shield(body), timeout=self.drain_timeout)
        except TimeoutError:
            logger.error(
                "%s %s: the run body did not return within %ss of the drain request; abandoning the wait so "
                "teardown can run",
                self.label,
                runtime.name,
                self.drain_timeout,
            )
            return True
        return False

    async def _release(self, runtime: BackendRuntime, *, propagating: bool) -> None:
        """Release the runtime's engine resources on the way out of ``launch``.

        ``aclose`` runs on every exit path, but its failure must not outrank the
        reason the launch is unwinding: while an exception is propagating, an
        ``aclose`` failure is REPORTED and swallowed so the original cause still
        reaches the caller. On the clean path there is no cause to protect, so a
        failure propagates like any other.

        A cancellation out of ``aclose`` after an ABANDONED drain is expected
        noise — the engine was still holding the loop when the wait was given up —
        so it is reported at WARNING and absorbed. A cancellation WITHOUT an
        overrun is a genuine one, arriving while teardown ran, and is RE-RAISED:
        a cancellation is a control-flow signal the caller must still see, and the
        reason the launch was unwinding survives on it as ``__context__``.
        """
        try:
            await runtime.aclose()
        except BaseException as exc:
            if not propagating:
                raise
            if isinstance(exc, asyncio.CancelledError):
                if not self._drain_overran:
                    raise
                logger.warning(
                    "%s %s: releasing engine resources was cancelled after the drain overran",
                    self.label,
                    runtime.name,
                )
                return
            logger.error(
                "%s %s: releasing engine resources failed while the launch was already unwinding",
                self.label,
                runtime.name,
                exc_info=exc,
            )

    # -- stopping ------------------------------------------------------------

    @contextmanager
    def _drain_wiring(self, runtime: BackendRuntime) -> Iterator[None]:
        """Subscribe warm-then-cold shutdown to SIGTERM and SIGINT for the span
        of the run body.

        Through the composed chain, so the host's own subscriber — the one whose
        main-task cancellation guarantees teardown — survives this registration
        and fires too. That cancellation is what reaches :meth:`_run_body`.
        """
        delivered = 0

        def _on_signal(sig: signal.Signals) -> None:
            nonlocal delivered
            delivered += 1
            if delivered == 1:
                logger.warning("%s %s: warm shutdown requested (%s)", self.label, runtime.name, sig.name)
                runtime.request_drain()
            else:
                logger.warning("%s %s: cold shutdown requested (%s)", self.label, runtime.name, sig.name)
                runtime.request_terminate()

        subscriptions: list[SignalSubscription] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                subscriptions.append(
                    signal_chain.add(sig, partial(_on_signal, sig), name=f"{self.label}-{runtime.name}-drain")
                )
            except (RuntimeError, ValueError, NotImplementedError) as exc:
                # Signal handlers need the main thread (and POSIX); without them
                # shutdown comes via task cancellation, which still drains.
                logger.warning("cannot install %s handler for %s shutdown: %s", sig.name, self.label, exc)
        try:
            yield
        finally:
            for subscription in subscriptions:
                subscription.cancel()

    # -- pool turnover -------------------------------------------------------

    def _install_turnover(self, runtime: BackendRuntime) -> None:
        """Wire a pool turnover into every registry-mutating fleet op, for a
        runtime whose workers hold a snapshot of this process's tool registry.

        The handler runs inside the op's apply, before its terminal reply, so a
        raise makes that op report ``failed`` rather than a false ``applied``.
        """
        if not runtime.pool_turnover_required:
            return

        async def _on_fleet_op_applied(op_name: str) -> None:
            if op_name not in REGISTRY_MUTATING_FLEET_OPS:
                return
            self._refresh_manifest_env()
            await runtime.turn_over_pool(reason=op_name, budget=_turnover_budget())

        tai42_app.lifecycle.on_fleet_op_applied(_on_fleet_op_applied)

    def _refresh_manifest_env(self) -> None:
        """Publish the live manifest into the env so the replacement workers
        inherit the post-mutation registry."""
        os.environ[self.dispatch_settings.manifest_key] = json.dumps(
            tai42_app.admin.live_manifest, separators=(",", ":")
        )


def _turnover_budget() -> float:
    """The whole-turnover budget: the bus apply window less a margin, floored. A
    malformed value raises — a guessed budget would report a false outcome."""
    raw = os.environ.get(BUS_APPLY_TIMEOUT_ENV)
    apply_timeout = BUS_APPLY_TIMEOUT_DEFAULT if raw is None else float(raw)
    return max(_TURNOVER_BUDGET_FLOOR, apply_timeout - _TURNOVER_BUDGET_MARGIN)
