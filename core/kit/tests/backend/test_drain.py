"""Warm drain on a recycle — the seam the whole design turns on.

A recycle self-sends SIGTERM. Through the composed chain that ONE signal now
reaches two subscribers: the runtime's drain request and the launcher's
main-task cancellation. The cancellation unwinds the coroutine driving the run
body, so unless the body is shielded and awaited to completion the recycle
SEVERS in-flight work instead of draining it — and the failure looks like a
clean exit.

Every test here asserts on ``"drained"``, which a run body appends only when it
RETURNED. A severed body never records it.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
import time
from collections.abc import Mapping
from typing import ClassVar

import pytest
from tai42_contract.backend.runtime import BackendRuntime

from tai42_kit.signals import signal_chain
from tests.backend.fakes import (
    FakeApp,
    FakeForkingBackend,
    FakeOnLoopBackend,
    FakeOnLoopReporter,
    FakeOnLoopWorker,
    FakeThreadReporter,
    FakeThreadWorker,
    RecordingRuntime,
    running_runtime,
)

_BACKENDS = [
    pytest.param(FakeOnLoopBackend, FakeOnLoopWorker, id="on-loop"),
    pytest.param(FakeForkingBackend, FakeThreadWorker, id="worker-thread"),
]

# The other half of the mode grid: a runtime driven off the inline path that
# pulls no work, so none of the drain machinery may touch it.
_REPORTERS = [
    pytest.param(FakeOnLoopBackend, FakeOnLoopReporter, id="on-loop"),
    pytest.param(FakeForkingBackend, FakeThreadReporter, id="worker-thread"),
]


# A launch-subcommand map, annotated so a test class declares it as the ClassVar
# the base already types it as.
_Runtimes = Mapping[str, type[BackendRuntime]]


@pytest.mark.parametrize(("backend_cls", "runtime_cls"), _BACKENDS)
async def test_recycle_signal_drains_the_body_before_the_cancellation_unwinds(
    backend_cls: type[FakeOnLoopBackend], runtime_cls: type[RecordingRuntime], app: FakeApp
) -> None:
    backend = backend_cls()
    task = asyncio.create_task(backend.launch(["worker"]))
    # The launcher subscribes FIRST (it owns the process), so the chain delivers
    # the runtime's drain innermost-first and the cancellation after it — exactly
    # the ordering a real recycle produces.
    launcher = signal_chain.add(signal.SIGTERM, task.cancel, name="backend-main-task-cancel")
    try:
        app.lifecycle.mark_ready()
        runtime = await running_runtime(runtime_cls)

        signal_chain._dispatch(signal.SIGTERM)

        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        launcher.cancel()

    # The body RETURNED: the shield kept the cancellation from severing it.
    assert "drained" in runtime.calls
    # The drain was requested by the signal AND again on the cancellation path;
    # the contract makes that idempotent, so both are expected.
    assert runtime.calls.count("request_drain") == 2
    # Teardown ran last, after the body was out.
    assert runtime.calls.index("drained") < runtime.calls.index("aclose")
    assert runtime.calls[-1] == "aclose"
    assert task.cancelled()


@pytest.mark.parametrize(("backend_cls", "runtime_cls"), _BACKENDS)
async def test_cancellation_without_a_signal_still_drains(
    backend_cls: type[FakeOnLoopBackend], runtime_cls: type[RecordingRuntime], app: FakeApp
) -> None:
    # The cancellation is the only mover here (no signal), which is the shape a
    # host takes when it stops a backend from its own shutdown path.
    backend = backend_cls()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(runtime_cls)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.calls.count("request_drain") == 1
    assert "drained" in runtime.calls
    assert runtime.calls[-1] == "aclose"


@pytest.mark.parametrize(("backend_cls", "runtime_cls"), _BACKENDS)
async def test_an_overrunning_drain_is_reported_and_abandoned(
    backend_cls: type[FakeOnLoopBackend],
    runtime_cls: type[RecordingRuntime],
    app: FakeApp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Bounded and loud: a wedged engine must not hold teardown open, so the wait
    # is abandoned with an ERROR and the supervisor's kill becomes the backstop.
    class _ImpatientBackend(backend_cls):  # type: ignore[valid-type, misc]
        drain_timeout = 0.05

    backend = _ImpatientBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(runtime_cls)
    runtime.drain_stalls = True

    with caplog.at_level(logging.ERROR, logger="tai42_kit.backend.base"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The budget is reported as configured, not rounded into a different number.
    assert "did not return within 0.05s of the drain request" in caplog.text
    assert runtime.calls.count("request_drain") == 1
    # The launch still unwound: teardown ran even though the body never left.
    assert runtime.calls[-1] == "aclose"
    assert "drained" not in runtime.calls

    # Let the abandoned body finish so it does not outlive the test.
    runtime.release()
    await asyncio.to_thread(_settle)


@pytest.mark.parametrize(("backend_cls", "runtime_cls"), _BACKENDS)
async def test_a_repeated_signal_escalates_from_drain_to_terminate(
    backend_cls: type[FakeOnLoopBackend], runtime_cls: type[RecordingRuntime], app: FakeApp
) -> None:
    backend = backend_cls()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(runtime_cls)
    # An engine that does not honor the warm drain: only the cold stop ends it.
    runtime.drain_stalls = True

    signal_chain._dispatch(signal.SIGTERM)
    assert runtime.drain_requests == 1
    assert runtime.terminate_requests == 0

    signal_chain._dispatch(signal.SIGTERM)

    await asyncio.wait_for(task, timeout=5)
    assert runtime.terminate_requests == 1
    assert "drained" in runtime.calls


async def test_a_runtime_with_no_cold_stop_keeps_draining(app: FakeApp, caplog: pytest.LogCaptureFixture) -> None:
    # The contract's default cold stop reports the gap and lets the warm drain
    # run out, rather than raising into the host's signal path.
    class _NoColdStopWorker(FakeOnLoopWorker):
        request_terminate = BackendRuntime.request_terminate

    class _NoColdStopBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _NoColdStopWorker}

    backend = _NoColdStopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(_NoColdStopWorker)
    runtime.drain_stalls = True

    with caplog.at_level(logging.WARNING):
        signal_chain._dispatch(signal.SIGTERM)
        signal_chain._dispatch(signal.SIGTERM)

    assert "declares no cold stop" in caplog.text
    assert "cold shutdown requested (SIGTERM)" in caplog.text
    runtime.release()
    await asyncio.wait_for(task, timeout=5)


async def test_sigint_is_wired_alongside_sigterm(app: FakeApp) -> None:
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeOnLoopWorker)

    assert signal_chain.subscribers(signal.SIGINT) == ("fake-on-loop-worker-drain",)
    signal_chain._dispatch(signal.SIGINT)

    await asyncio.wait_for(task, timeout=5)
    assert runtime.drain_requests == 1


async def test_the_wiring_leaves_the_chain_when_the_body_is_out(app: FakeApp) -> None:
    # The subscriptions are scoped to the run body: leaving them behind would
    # keep a dead runtime's drain in every later signal's delivery.
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeOnLoopWorker)
    assert signal_chain.subscribers(signal.SIGTERM) == ("fake-on-loop-worker-drain",)

    runtime.release()
    await asyncio.wait_for(task, timeout=5)

    assert signal_chain.subscribers(signal.SIGTERM) == ()


async def test_wiring_survives_an_environment_that_refuses_signal_handlers(
    app: FakeApp, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Off the main thread (or off POSIX) there is no handler to install; shutdown
    # then arrives as a cancellation, which still drains.
    loop = asyncio.get_running_loop()

    def _refuse(sig, callback, *args):
        raise RuntimeError("add_signal_handler() can only be called from the main thread")

    monkeypatch.setattr(loop, "add_signal_handler", _refuse)
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    with caplog.at_level(logging.WARNING, logger="tai42_kit.backend.base"):
        runtime = await running_runtime(FakeOnLoopWorker)

    assert "cannot install SIGTERM handler for fake-on-loop shutdown" in caplog.text
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "drained" in runtime.calls


def _settle() -> None:
    """Give an abandoned daemon body a moment to finish off-loop."""
    for _ in range(100):
        if threading.active_count() <= 2:
            return
        time.sleep(0.01)


# -- the non-consuming half of the grid ---------------------------------------


@pytest.mark.parametrize(("backend_cls", "runtime_cls"), _REPORTERS)
async def test_cancelling_a_non_consuming_runtime_ends_it_cleanly(
    backend_cls: type[FakeOnLoopBackend], runtime_cls: type[RecordingRuntime], app: FakeApp
) -> None:
    # A runtime that pulls no work holds no in-flight work and declares no
    # ``request_drain``. Routing it through the drain path would call the
    # contract's raising default and turn a clean stop into a NotImplementedError
    # that swallows the cancellation.
    backend = backend_cls()
    task = asyncio.create_task(backend.launch(["reporter"]))
    runtime = await running_runtime(runtime_cls)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert runtime.drain_requests == 0
    assert runtime.terminate_requests == 0
    assert runtime.calls[-1] == "aclose"
    # The body never RETURNED — a cancellation is not a drain, and nothing here
    # waits for in-flight work that by declaration does not exist.
    assert "drained" not in runtime.calls
    # It still built (that is every runtime's own step); only the readiness gate
    # and the drain wiring are the consumer's.
    assert runtime.calls[0] == "build"
    assert signal_chain.subscribers(signal.SIGTERM) == ()

    runtime.release()
    await asyncio.to_thread(_settle)


async def test_a_cancelled_non_consuming_loop_body_receives_the_cancellation(app: FakeApp) -> None:
    # Awaited, not shielded: the cancellation reaches the body itself, which is
    # the only stop a non-consuming runtime has.
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["reporter"]))
    runtime = await running_runtime(FakeOnLoopReporter)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "body_cancelled" in runtime.calls


async def test_a_non_consuming_body_that_returns_closes_cleanly(app: FakeApp) -> None:
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["reporter"]))
    runtime = await running_runtime(FakeOnLoopReporter)

    runtime.release()
    await asyncio.wait_for(task, timeout=5)

    assert runtime.calls == ["build", "run", "drained", "aclose"]


# -- releasing engine resources on the way out --------------------------------


class _AcloseFails(FakeOnLoopWorker):
    async def aclose(self) -> None:
        await super().aclose()
        raise RuntimeError("the connection would not close")


class _AcloseCancels(FakeOnLoopWorker):
    async def aclose(self) -> None:
        await super().aclose()
        raise asyncio.CancelledError


async def test_a_failing_aclose_never_replaces_the_reason_the_launch_is_unwinding(
    app: FakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    # The body's failure is what an operator has to see; a teardown failure on top
    # of it is a second fact, reported, not a substitute for the first.
    class _Worker(_AcloseFails):
        def __init__(self, args) -> None:
            super().__init__(args)
            self.body_error = RuntimeError("the engine died mid-run")

    class _Backend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _Worker}

    app.lifecycle.mark_ready()
    with (
        caplog.at_level(logging.ERROR, logger="tai42_kit.backend.base"),
        pytest.raises(RuntimeError, match="the engine died mid-run"),
    ):
        await _Backend().launch(["worker"])

    assert "releasing engine resources failed while the launch was already unwinding" in caplog.text
    assert "the connection would not close" in caplog.text


async def test_a_failing_aclose_on_the_clean_path_propagates(app: FakeApp) -> None:
    # Nothing is being unwound here, so there is no cause to protect: an engine
    # that cannot release its resources is the failure.
    class _Backend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _AcloseFails}

    task = asyncio.create_task(_Backend().launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(_AcloseFails)
    runtime.release()

    with pytest.raises(RuntimeError, match="the connection would not close"):
        await asyncio.wait_for(task, timeout=5)


async def test_a_cancelled_aclose_after_an_overrun_is_expected_noise(
    app: FakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    # The engine was still holding the loop when the drain wait was given up, so a
    # cancellation out of its teardown says nothing new — WARNING, not ERROR, and
    # the launch still ends as the cancellation it was.
    class _Backend(FakeOnLoopBackend):
        drain_timeout = 0.05
        runtimes: ClassVar[_Runtimes] = {"worker": _AcloseCancels}

    task = asyncio.create_task(_Backend().launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(_AcloseCancels)
    runtime.drain_stalls = True

    with caplog.at_level(logging.DEBUG, logger="tai42_kit.backend.base"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("releasing engine resources was cancelled after the drain overran" in r.message for r in warnings)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and "releasing engine" in r.message]

    runtime.release()
    await asyncio.to_thread(_settle)


async def test_a_thread_body_the_cancellation_cannot_stop_is_reported(
    app: FakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    # Cancelling the bridge ends the AWAIT, never the thread. Nothing here can stop
    # a thread already inside the run body, so it is abandoned as a daemon and said
    # out loud — the same bounded-and-loud discipline as an overrun drain.
    backend = FakeForkingBackend()
    task = asyncio.create_task(backend.launch(["reporter"]))
    runtime = await running_runtime(FakeThreadReporter)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.backend.base"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "cannot stop a run body already on its thread" in caplog.text
    assert "fake-forking reporter" in caplog.text
    assert "drained" not in runtime.calls

    runtime.release()
    await asyncio.to_thread(_settle)


async def test_a_loop_body_that_takes_the_cancellation_is_not_reported_as_abandoned(
    app: FakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    # The on-loop half of the same quadrant: the cancellation really does reach the
    # body, so there is nothing abandoned and nothing to report.
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["reporter"]))
    runtime = await running_runtime(FakeOnLoopReporter)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.backend.base"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "body_cancelled" in runtime.calls
    assert "abandoning it on its daemon thread" not in caplog.text


async def test_a_non_consuming_thread_body_that_raises_surfaces_on_the_loop(app: FakeApp) -> None:
    # The non-consuming quadrant gets the same bridge as the consuming one: a
    # thread's exception is carried back rather than dying with the thread.
    class _FailingReporter(FakeThreadReporter):
        def __init__(self, args) -> None:
            super().__init__(args)
            self.body_error = RuntimeError("the exporter died on its own thread")

    class _Backend(FakeForkingBackend):
        runtimes: ClassVar[_Runtimes] = {"reporter": _FailingReporter}

    with pytest.raises(RuntimeError, match="the exporter died on its own thread"):
        await asyncio.wait_for(_Backend().launch(["reporter"]), timeout=5)

    runtime = _FailingReporter.latest
    assert runtime is not None
    assert runtime.calls == ["build", "run", "aclose"]


async def test_a_genuine_cancellation_out_of_aclose_is_re_raised(app: FakeApp) -> None:
    # No overrun, so this cancellation is real: it arrived while teardown ran and
    # the caller must still see it. The reason the launch was unwinding survives on
    # it as ``__context__`` rather than being replaced.
    class _Worker(_AcloseCancels):
        def __init__(self, args) -> None:
            super().__init__(args)
            self.body_error = RuntimeError("the engine died mid-run")

    class _Backend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _Worker}

    app.lifecycle.mark_ready()
    with pytest.raises(asyncio.CancelledError) as exc:
        await _Backend().launch(["worker"])

    assert isinstance(exc.value.__context__, RuntimeError)
    assert str(exc.value.__context__) == "the engine died mid-run"


async def test_a_relaunch_after_an_overrun_starts_from_a_clean_drain_state(app: FakeApp) -> None:
    # ``_drain_overran`` decides whether a cancelled ``aclose`` is expected noise or
    # a real cancellation, so a launch that overran must not leave the next one
    # absorbing a genuine one.
    class _Backend(FakeOnLoopBackend):
        drain_timeout = 0.05
        runtimes: ClassVar[_Runtimes] = {"worker": FakeOnLoopWorker}

    backend = _Backend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    stalled = await running_runtime(FakeOnLoopWorker)
    stalled.drain_stalls = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend._drain_overran is True
    stalled.release()
    await asyncio.sleep(0)

    FakeOnLoopWorker.latest = None
    second = asyncio.create_task(backend.launch(["worker"]))
    runtime = await running_runtime(FakeOnLoopWorker)

    assert backend._drain_overran is False

    runtime.release()
    await asyncio.wait_for(second, timeout=5)
