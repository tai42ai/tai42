"""Two engine-free backends the whole lifecycle suite runs against.

The certification claim is that a binding writes ONLY its runtime classes and
gets the entire lifecycle. These fakes are that claim, executable: no vendor
library, no broker, no process — one on-loop backend shaped like an engine that
runs work in-process, and one worker-thread backend shaped like an engine whose
worker processes persist across jobs and must therefore be turned over.

Every runtime records the lifecycle calls it received, so a test asserts on what
the base DID rather than on what it logged.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, ClassVar, Self

from pydantic_settings import SettingsConfigDict
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode

from tai42_kit.backend import BackendDispatchSettings, ManagedBackend

# A launch-subcommand map, annotated so a test class declares it as the ClassVar
# the base already types it as.
_Runtimes = Mapping[str, type[BackendRuntime]]


class FakeDispatchSettings(BackendDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="FAKEBACKEND_")


class RecordingRuntime(BackendRuntime):
    """Lifecycle bookkeeping shared by every fake runtime.

    ``latest`` is set by ``from_args`` on the concrete subclass, which is how a
    test reaches the instance the base built for itself inside ``launch``.
    """

    latest: ClassVar[Self | None] = None

    def __init__(self, args: Sequence[str]) -> None:
        self.args = list(args)
        self.calls: list[str] = []
        self.drain_requests = 0
        self.terminate_requests = 0
        self.turnovers: list[tuple[str, float]] = []
        # The env as it stood when the turnover was asked for: the replacement
        # workers inherit it, so the refresh has to have landed by then.
        self.env_at_turnover: dict[str, str] = {}
        self.body_thread: threading.Thread | None = None
        self.started = threading.Event()
        # Injected failures / stalls, so a test drives the pathological paths
        # without a real engine to wedge.
        self.body_error: BaseException | None = None
        self.turnover_error: BaseException | None = None
        self.drain_stalls = False

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        runtime = cls(args)
        cls.latest = runtime
        return runtime

    async def build(self) -> None:
        self.calls.append("build")

    async def aclose(self) -> None:
        self.calls.append("aclose")

    def request_drain(self) -> None:
        self.calls.append("request_drain")
        self.drain_requests += 1
        if not self.drain_stalls:
            self.release()

    def request_terminate(self) -> None:
        self.calls.append("request_terminate")
        self.terminate_requests += 1
        self.release()

    def release(self) -> None:
        """Let the run body return. Overridden per mode — the stop primitive
        differs between a loop body and a thread body."""
        raise NotImplementedError

    def _enter_body(self) -> None:
        self.calls.append("run")
        self.body_thread = threading.current_thread()
        self.started.set()
        if self.body_error is not None:
            raise self.body_error

    def _leave_body(self) -> None:
        """Recorded separately from the entry: a body that RETURNED drained, while
        one that was severed by a cancellation never gets here."""
        self.calls.append("drained")


class FakeOnLoopWorker(RecordingRuntime):
    """An engine that runs work in-process on the serving loop, so a registry
    mutation reaches it directly and no pool has to be turned over."""

    name = "worker"
    mode = ExecutionMode.on_loop
    consumes_work = True

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self._stopped = asyncio.Event()

    async def run_on_loop(self) -> None:
        self._enter_body()
        await self._stopped.wait()
        self._leave_body()

    def release(self) -> None:
        self._stopped.set()


class FakeThreadWorker(RecordingRuntime):
    """An engine whose worker processes persist across jobs: they hold a snapshot
    of this process's tool registry, so a mutation obliges a pool turnover."""

    name = "worker"
    mode = ExecutionMode.worker_thread
    consumes_work = True
    pool_turnover_required = True

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self._stopped = threading.Event()

    def run_blocking(self) -> None:
        self._enter_body()
        self._stopped.wait()
        self._leave_body()

    def release(self) -> None:
        self._stopped.set()

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        self.calls.append("turn_over_pool")
        self.turnovers.append((reason, budget))
        self.env_at_turnover = dict(os.environ)
        if self.turnover_error is not None:
            raise self.turnover_error


class FakeSchedulerRuntime(RecordingRuntime):
    """A non-consuming runtime that blocks the serving loop on purpose: nothing
    else runs on it, so its engine keeps its own signal disposition."""

    name = "scheduler"
    mode = ExecutionMode.inline

    def run_blocking(self) -> None:
        self._enter_body()
        self._leave_body()


class FakeOnLoopReporter(RecordingRuntime):
    """Non-consuming, but driven on the loop rather than inline — a dashboard or
    a metrics exporter. It pulls no work and declares no drain, so a cancellation
    is simply its stop."""

    name = "reporter"
    mode = ExecutionMode.on_loop

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self._stopped = asyncio.Event()

    async def run_on_loop(self) -> None:
        self._enter_body()
        try:
            await self._stopped.wait()
        except asyncio.CancelledError:
            self.calls.append("body_cancelled")
            raise
        self._leave_body()

    def release(self) -> None:
        self._stopped.set()


class FakeThreadReporter(RecordingRuntime):
    """Non-consuming on a blocking body — the same quadrant as
    :class:`FakeOnLoopReporter`, off the loop."""

    name = "reporter"
    mode = ExecutionMode.worker_thread

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self._stopped = threading.Event()

    def run_blocking(self) -> None:
        self._enter_body()
        self._stopped.wait()
        self._leave_body()

    def release(self) -> None:
        self._stopped.set()


class FakeOnLoopBackend(ManagedBackend):
    label = "fake-on-loop"
    runtimes: ClassVar[_Runtimes] = {
        "worker": FakeOnLoopWorker,
        "scheduler": FakeSchedulerRuntime,
        "reporter": FakeOnLoopReporter,
    }


class FakeForkingBackend(ManagedBackend):
    label = "fake-forking"
    runtimes: ClassVar[_Runtimes] = {
        "worker": FakeThreadWorker,
        "scheduler": FakeSchedulerRuntime,
        "reporter": FakeThreadReporter,
    }

    @property
    def dispatch_settings(self) -> BackendDispatchSettings:
        return FakeDispatchSettings()


class FakeLifecycle:
    """The two lifecycle members the base reaches for: the boot-readiness latch
    and the post-apply fleet-op hook."""

    def __init__(self) -> None:
        self._ready = asyncio.Event()
        self.fleet_handlers: list[Any] = []

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def mark_ready(self) -> None:
        self._ready.set()

    def on_fleet_op_applied(self, func: Any) -> Any:
        self.fleet_handlers.append(func)
        return func


class FakeApp:
    """The forwarding target ``tai42_app`` binds to for the duration of a test."""

    def __init__(self, live_manifest: dict[str, Any] | None = None) -> None:
        self.lifecycle = FakeLifecycle()
        self.admin = SimpleNamespace(live_manifest=live_manifest if live_manifest is not None else {"tools": []})


async def running_runtime(runtime_cls: type[RecordingRuntime], timeout: float = 5.0) -> RecordingRuntime:
    """The instance ``launch`` built for itself, once its run body is inside.

    The wait is off-loop: an on-loop body only reaches its entry when the serving
    loop is free, so blocking here would deadlock against the thing being awaited.
    """
    deadline = time.monotonic() + timeout
    while runtime_cls.latest is None:
        assert time.monotonic() < deadline, f"{runtime_cls.__name__} was never constructed"
        await asyncio.sleep(0)
    runtime = runtime_cls.latest
    assert await asyncio.to_thread(runtime.started.wait, max(0.0, deadline - time.monotonic()))
    return runtime
