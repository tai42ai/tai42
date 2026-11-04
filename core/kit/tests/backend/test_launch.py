"""The launch template: dispatch, declaration validation, readiness, run modes.

These are the parts a vendor binding stops writing. Each one is asserted against
the fakes rather than against a real engine, because the whole point is that the
behavior does not depend on which engine is underneath.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

import pytest
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode, ProgressLiveness

from tai42_kit.backend import DEFAULT_DRAIN_TIMEOUT_SECONDS, BackendDispatchSettings, ManagedBackend
from tai42_kit.signals import STOP_SIGNALS, signal_chain

from .fakes import (
    FakeApp,
    FakeForkingBackend,
    FakeOnLoopBackend,
    FakeOnLoopWorker,
    FakeSchedulerRuntime,
    FakeThreadWorker,
    running_runtime,
)

# A launch-subcommand map, annotated so a test class declares it as the ClassVar
# the base already types it as.
_Runtimes = Mapping[str, type[BackendRuntime]]


# -- dispatch ----------------------------------------------------------------


async def test_no_subcommand_names_every_runtime() -> None:
    with pytest.raises(ValueError, match=r"fake-on-loop launch requires a subcommand: worker, scheduler"):
        await FakeOnLoopBackend().launch([])


async def test_an_unknown_subcommand_names_every_runtime() -> None:
    with pytest.raises(ValueError, match=r"Unknown fake-on-loop launch subcommand 'flush'"):
        await FakeOnLoopBackend().launch(["flush", "--now"])

    with pytest.raises(ValueError, match=r"expected one of: worker, scheduler"):
        await FakeOnLoopBackend().launch(["flush"])


async def test_the_remaining_args_reach_the_runtimes_own_parser() -> None:
    # The subcommand selects the runtime; everything after it is the runtime's
    # own option surface, which only the binding knows how to parse.
    await FakeOnLoopBackend().launch(["scheduler", "--interval", "5"])

    assert FakeSchedulerRuntime.latest is not None
    assert FakeSchedulerRuntime.latest.args == ["--interval", "5"]


# -- declaration validation ---------------------------------------------------


class _Bare(BackendRuntime):
    """A runtime with nothing but the required parser, so each red case below
    declares exactly one thing and honors none of it."""

    name = "worker"
    liveness = ProgressLiveness()

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls()


class _NoLiveness(BackendRuntime):
    name = "worker"
    mode = ExecutionMode.on_loop

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls()

    async def run_on_loop(self) -> None:
        return None


class _OnLoopWithNoBody(_Bare):
    mode = ExecutionMode.on_loop


class _BlockingWithNoBody(_Bare):
    mode = ExecutionMode.worker_thread


class _ConsumesWithNoDrain(_Bare):
    mode = ExecutionMode.on_loop
    consumes_work = True

    async def run_on_loop(self) -> None:
        return None


class _ConsumesInline(_Bare):
    mode = ExecutionMode.inline
    consumes_work = True

    def run_blocking(self) -> None:
        return None

    def request_drain(self) -> None:
        return None


class _TurnoverWithNoBinding(FakeOnLoopWorker):
    pool_turnover_required = True


class _TurnoverWithoutConsuming(_Bare):
    mode = ExecutionMode.on_loop
    pool_turnover_required = True

    async def run_on_loop(self) -> None:
        return None

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        return None


class _TurnoverDecidedByAnOption(FakeOnLoopWorker):
    """Declares the capability per INSTANCE, the way a launch option would."""

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        runtime = super().from_args(args)
        runtime.pool_turnover_required = True
        return runtime


def _backend_for(runtime_cls: type[BackendRuntime]) -> ManagedBackend:
    class _Backend(ManagedBackend):
        label = "under-test"
        runtimes: ClassVar[_Runtimes] = {"worker": runtime_cls}

    return _Backend()


@pytest.mark.parametrize(
    ("runtime_cls", "expected"),
    [
        (_NoLiveness, "declares no liveness, so the host cannot tell how it signals it is alive"),
        (_OnLoopWithNoBody, "declares mode=on_loop but implements no run_on_loop"),
        (_BlockingWithNoBody, "declares mode=worker_thread but implements no run_blocking"),
        (_ConsumesWithNoDrain, "declares consumes_work but implements no request_drain"),
        (_ConsumesInline, "declares consumes_work with mode=inline"),
        (_TurnoverWithNoBinding, "declares pool_turnover_required but implements no turn_over_pool"),
        (_TurnoverWithoutConsuming, "declares pool_turnover_required without consumes_work"),
        (_TurnoverDecidedByAnOption, "declares pool_turnover_required but implements no turn_over_pool"),
    ],
)
async def test_an_unhonored_declaration_refuses_the_launch(runtime_cls: type[BackendRuntime], expected: str) -> None:
    with pytest.raises(TypeError, match="under-test launch refused") as exc:
        await _backend_for(runtime_cls).launch(["worker"])

    assert expected in str(exc.value)


async def test_a_backend_that_re_implements_the_template_is_refused() -> None:
    # Delegating to the template does not buy back the guarantees: whatever the
    # override does around it is exactly the per-backend divergence the base exists
    # to remove.
    class _WrappingBackend(FakeOnLoopBackend):
        async def launch(self, args: Sequence[str]) -> None:
            await super().launch(args)

    with pytest.raises(TypeError, match="overrides launch"):
        await _WrappingBackend().launch(["worker"])


async def test_a_runtime_naming_its_missing_binding_names_itself() -> None:
    with pytest.raises(TypeError, match=r"_OnLoopWithNoBody declares mode=on_loop"):
        await _backend_for(_OnLoopWithNoBody).launch(["worker"])


# -- readiness ----------------------------------------------------------------


async def test_a_boot_that_never_completes_refuses_to_consume(app: FakeApp) -> None:
    class _ImpatientBackend(FakeOnLoopBackend):
        ready_timeout = 0.05

    with pytest.raises(RuntimeError, match=r"fake-on-loop worker: the app did not become ready within 0\.05s"):
        await _ImpatientBackend().launch(["worker"])

    runtime = FakeOnLoopWorker.latest
    assert runtime is not None
    # Engine objects were built (building is not consuming) and released, but the
    # body never started against a half-built registry.
    assert runtime.calls == ["build", "aclose"]


async def test_a_ready_app_lets_the_body_run(app: FakeApp) -> None:
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    await asyncio.sleep(0)
    runtime = FakeOnLoopWorker.latest
    assert runtime is not None
    assert not runtime.started.is_set()

    app.lifecycle.mark_ready()
    assert await running_runtime(FakeOnLoopWorker) is runtime

    runtime.release()
    await asyncio.wait_for(task, timeout=5)
    assert runtime.calls == ["build", "run", "drained", "aclose"]


# -- run modes ----------------------------------------------------------------


async def test_an_on_loop_body_runs_on_the_serving_loop(app: FakeApp) -> None:
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeOnLoopWorker)

    assert runtime.body_thread is threading.main_thread()

    runtime.release()
    await asyncio.wait_for(task, timeout=5)


async def test_a_blocking_body_runs_on_a_dedicated_daemon_thread(app: FakeApp) -> None:
    # Deliberately not a default-executor thread: a wedged engine there hangs
    # ``asyncio.run``'s executor shutdown at interpreter exit, so the process a
    # supervisor is stopping never exits. A daemon thread cannot.
    backend = FakeForkingBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeThreadWorker)

    thread = runtime.body_thread
    assert thread is not None
    assert thread.daemon is True
    assert thread.name == "tai-backend-fake-forking-worker"
    assert thread is not threading.main_thread()

    runtime.release()
    await asyncio.wait_for(task, timeout=5)


async def test_an_inline_body_blocks_the_loop_and_is_wired_to_nothing() -> None:
    # A non-consuming inline runtime keeps its engine's own signal disposition:
    # nothing else runs on that loop, so there is nothing to compose with.
    class _ObservingScheduler(FakeSchedulerRuntime):
        observed: tuple[str, ...] = ()

        def run_blocking(self) -> None:
            type(self).observed = signal_chain.subscribers(signal.SIGTERM)
            super().run_blocking()

    class _SchedulerBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"scheduler": _ObservingScheduler}

    await _SchedulerBackend().launch(["scheduler"])

    runtime = _ObservingScheduler.latest
    assert runtime is not None
    assert _ObservingScheduler.observed == ()
    assert runtime.body_thread is threading.main_thread()
    assert runtime.calls == ["build", "run", "drained", "aclose"]


# How long the interruptible inline body waits for its interrupt before returning on its own.
_INLINE_BODY_BOUND_SECONDS = 5.0


async def test_an_inline_body_runs_under_the_default_dispositions_of_the_stop_signals() -> None:
    # A host subscribes the stop signals on the chain before it launches. An inline body blocks
    # the loop, so no chain subscriber could run until it returned; for its span the stop signals
    # carry the process's default dispositions, and a Ctrl-C reaches the engine as the
    # ``KeyboardInterrupt`` it ends on.
    body_started = threading.Event()

    class _InterruptibleScheduler(FakeSchedulerRuntime):
        interrupted = False
        dispositions: tuple[object, object] = (None, None)

        def run_blocking(self) -> None:
            cls = type(self)
            cls.dispositions = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
            try:
                self._enter_body()
                body_started.set()
                threading.Event().wait(timeout=_INLINE_BODY_BOUND_SECONDS)
            except KeyboardInterrupt:
                cls.interrupted = True
            self._leave_body()

    class _SchedulerBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"scheduler": _InterruptibleScheduler}

    def _interrupt_once_the_body_runs() -> None:
        if body_started.wait(timeout=_INLINE_BODY_BOUND_SECONDS):
            os.kill(os.getpid(), signal.SIGINT)

    host_fired = asyncio.Event()
    host = [signal_chain.add(sig, host_fired.set, name="host-stop") for sig in STOP_SIGNALS]
    try:
        sender = threading.Thread(target=_interrupt_once_the_body_runs, name="interrupt-sender")
        sender.start()
        await _SchedulerBackend().launch(["scheduler"])
        sender.join()

        runtime = _InterruptibleScheduler.latest
        assert runtime is not None
        assert _InterruptibleScheduler.interrupted is True
        assert _InterruptibleScheduler.dispositions == (signal.default_int_handler, signal.SIG_DFL)
        assert runtime.calls == ["build", "run", "drained", "aclose"]
        # The interrupt went to the engine, not to the host's subscriber.
        assert not host_fired.is_set()

        # The chain took the stop signals back once the body returned: a real delivery reaches
        # the host's subscriber again.
        assert _chain_signals_installed()
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(host_fired.wait(), timeout=5)
    finally:
        for subscription in host:
            subscription.cancel()


def _chain_signals_installed() -> bool:
    """Whether every stop signal carries the chain's handler rather than a default disposition."""
    return all(signal.getsignal(sig) not in (signal.SIG_DFL, signal.default_int_handler) for sig in STOP_SIGNALS)


async def test_a_non_consuming_runtime_skips_the_readiness_gate_but_still_builds() -> None:
    # No app is bound at all here: a runtime that pulls no work never touches the
    # readiness latch, so it must not reach for the app handle. Building is not
    # gating — it is every runtime's own step — so it still runs.
    await FakeOnLoopBackend().launch(["scheduler"])

    runtime = FakeSchedulerRuntime.latest
    assert runtime is not None
    assert runtime.calls == ["build", "run", "drained", "aclose"]


# -- teardown -----------------------------------------------------------------


async def test_engine_resources_are_released_when_the_body_raises() -> None:
    class _FailingScheduler(FakeSchedulerRuntime):
        def __init__(self, args: Sequence[str]) -> None:
            super().__init__(args)
            self.body_error = RuntimeError("engine refused to start")

    class _FailingBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"scheduler": _FailingScheduler}

    with pytest.raises(RuntimeError, match="engine refused to start"):
        await _FailingBackend().launch(["scheduler"])

    runtime = _FailingScheduler.latest
    assert runtime is not None
    assert runtime.calls == ["build", "run", "aclose"]


async def test_a_blocking_body_that_raises_surfaces_on_the_loop(app: FakeApp) -> None:
    # Nothing above a thread's frame can see its exception, so the bridge carries
    # it back to the launch coroutine rather than letting it die with the thread.
    class _FailingThreadWorker(FakeThreadWorker):
        def __init__(self, args: Sequence[str]) -> None:
            super().__init__(args)
            self.body_error = RuntimeError("the engine died on its own thread")

    class _FailingBackend(FakeForkingBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _FailingThreadWorker}

    task = asyncio.create_task(_FailingBackend().launch(["worker"]))
    app.lifecycle.mark_ready()

    with pytest.raises(RuntimeError, match="the engine died on its own thread"):
        await asyncio.wait_for(task, timeout=5)

    runtime = _FailingThreadWorker.latest
    assert runtime is not None
    assert runtime.calls == ["build", "run", "aclose"]


async def test_engine_resources_are_released_when_the_build_raises(app: FakeApp) -> None:
    class _FailingBuildWorker(FakeOnLoopWorker):
        async def build(self) -> None:
            raise RuntimeError("could not reach the broker")

    class _FailingBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _FailingBuildWorker}

    with pytest.raises(RuntimeError, match="could not reach the broker"):
        await _FailingBackend().launch(["worker"])

    runtime = _FailingBuildWorker.latest
    assert runtime is not None
    assert runtime.calls == ["aclose"]


# -- budgets ------------------------------------------------------------------


def test_the_default_drain_budget_covers_the_default_job_budget() -> None:
    # A warm drain waits for in-flight work to finish, so a bound below the job
    # budget abandons — and severs — exactly the work it exists to protect.
    assert BackendDispatchSettings().task_timeout < DEFAULT_DRAIN_TIMEOUT_SECONDS
    assert FakeOnLoopBackend().drain_timeout == DEFAULT_DRAIN_TIMEOUT_SECONDS


def test_the_default_drain_budget_is_the_dispatch_job_budget_default_plus_a_margin() -> None:
    from tai42_kit.backend import DRAIN_MARGIN_SECONDS

    job_budget_default = BackendDispatchSettings.model_fields["task_timeout"].default
    assert float(job_budget_default) + DRAIN_MARGIN_SECONDS == DEFAULT_DRAIN_TIMEOUT_SECONDS


def test_a_binding_can_read_its_drain_budget_from_live_settings() -> None:
    # ``drain_timeout`` is not a ClassVar precisely so a binding can override it
    # with a property that reads the engine's CURRENT job budget.
    budget = {"value": 42.0}

    class _LiveBudgetBackend(FakeOnLoopBackend):
        @property
        def drain_timeout(self) -> float:
            return budget["value"]

    backend = _LiveBudgetBackend()
    assert backend.drain_timeout == 42.0
    budget["value"] = 99.0
    assert backend.drain_timeout == 99.0
