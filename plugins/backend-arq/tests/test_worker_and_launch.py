"""The arq vendor binding: how ``ArqWorkerRuntime`` builds, runs, drains and
closes an :class:`arq.Worker`, and what ``ArqBackend.launch`` does with it.

The lifecycle AROUND the runtime (the readiness gate, the composed signal
wiring, drain-on-cancellation) belongs to ``tai42_kit.backend.ManagedBackend``
and is proven there; what is irreducibly arq's — the ``handle_signals=False``
handover, the warm/cold signal pair, the close-after-drain hazard — is proven
here, plus the few launch-level assertions that pin this backend into that base.
"""

from __future__ import annotations

import asyncio
import signal
import time
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock

import click
import pytest
from click.testing import CliRunner
from tai42_contract.backend.runtime import CONSUMING_RUNTIME, ExecutionMode

from tai42_backend_arq import worker
from tai42_backend_arq.backend import ArqBackend
from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.scheduler import recover_stalled_schedules
from tai42_backend_arq.worker import ArqWorkerRuntime


class _FakeWorker:
    """An arq worker that records how it was built and how it was driven."""

    instances: ClassVar[list[_FakeWorker]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ran = False
        self.closed = False
        # The signals the runtime asked arq's own handlers for, by handler.
        self.warm: list[signal.Signals] = []
        self.cold: list[signal.Signals] = []
        _FakeWorker.instances.append(self)

    @classmethod
    def latest(cls) -> _FakeWorker:
        assert cls.instances, "no arq worker was built"
        return cls.instances[-1]

    async def async_run(self) -> None:
        # A burst worker's run returns when the queue drains; model that, so a
        # launch completes and tears its resources down.
        self.ran = True

    async def close(self) -> None:
        self.closed = True

    def handle_sig_wait_for_completion(self, signum: signal.Signals) -> None:
        self.warm.append(signum)

    def handle_sig(self, signum: signal.Signals) -> None:
        self.cold.append(signum)


class _StubPool:
    """The ArqRedis handle a real run creates inside ``Worker.main``. Without one
    ``Worker.close`` returns before the teardown the close tests are about."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.closed = False

    async def delete(self, key: str) -> None:
        self.deleted.append(key)

    async def close(self, close_connection_pool: bool = False) -> None:
        self.closed = True


@pytest.fixture
def pool_close(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The shared ArqRedis pool's close, stubbed — the runtime owns closing it."""
    close = AsyncMock()
    monkeypatch.setattr(RedisPoolManager, "close", close)
    return close


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, pool_close: AsyncMock) -> type[_FakeWorker]:
    _FakeWorker.instances.clear()
    monkeypatch.setattr(worker, "Worker", _FakeWorker)
    return _FakeWorker


async def _built(*args: str) -> ArqWorkerRuntime:
    """The runtime the launch path would build, for the given option words."""
    runtime = ArqWorkerRuntime.from_args(args)
    await runtime.build()
    return runtime


async def _running(worker_env: type[_FakeWorker], timeout: float = 5.0) -> _FakeWorker:
    """The built worker, once its run body is inside. Polls rather than waits:
    the body runs on THIS loop, so blocking here would deadlock against it."""
    deadline = time.monotonic() + timeout
    while not (worker_env.instances and worker_env.latest().ran):
        assert time.monotonic() < deadline, "the arq worker never started running"
        await asyncio.sleep(0)
    return worker_env.latest()


# -- declarations ------------------------------------------------------------------


def test_runtime_declares_a_consuming_on_loop_worker() -> None:
    assert ArqWorkerRuntime.name == CONSUMING_RUNTIME
    assert ArqWorkerRuntime.mode is ExecutionMode.on_loop
    assert ArqWorkerRuntime.consumes_work is True
    # arq runs every job as a coroutine in THIS process, so a registry mutation
    # reaches the running jobs directly: no pool holds a stale snapshot.
    assert ArqWorkerRuntime.pool_turnover_required is False


def test_backend_declares_the_worker_subcommand() -> None:
    assert ArqBackend.label == "arq"
    assert dict(ArqBackend.runtimes) == {"worker": ArqWorkerRuntime}


# -- build -------------------------------------------------------------------------


async def test_build_constructs_the_worker_and_keeps_arq_off_the_signals(worker_env) -> None:
    """arq's ``Worker.__init__`` installs its own SIGINT/SIGTERM handlers unless
    told not to, and ``add_signal_handler`` is last-writer-wins: that install
    would destroy the host's composed chain, teardown handler and all."""
    await _built()

    kwargs = worker_env.latest().kwargs
    assert kwargs["handle_signals"] is False
    assert kwargs["allow_abort_jobs"] is True
    assert kwargs["on_startup"] is recover_stalled_schedules
    assert len(kwargs["functions"]) == 3
    assert kwargs["keep_result"] == 3600
    assert kwargs["max_jobs"] == 10


async def test_build_passes_zero_keep_result_and_burst_through(worker_env) -> None:
    # arq itself treats keep_result=0 as "keep nothing"; it must not be rewritten
    # (arq crashes on a None keep_result at job completion).
    await _built("--burst", "--keep-result", "0")

    assert worker_env.latest().kwargs["keep_result"] == 0
    assert worker_env.latest().kwargs["burst"] is True


async def test_build_applies_the_redis_url_override(worker_env) -> None:
    await _built("--redis-url", "redis://elsewhere:6390/3")

    redis_settings = worker_env.latest().kwargs["redis_settings"]
    assert redis_settings.host == "elsewhere"
    assert redis_settings.port == 6390
    assert redis_settings.database == 3


async def test_build_takes_the_warm_drain_window_from_settings(worker_env, monkeypatch) -> None:
    """The warm-drain window is the ``job_completion_wait`` setting, not a
    hardcoded constant: a non-zero value makes arq's warm handler drain in-flight
    jobs instead of cancelling them."""
    from tai42_backend_arq.settings import ArqSettings

    monkeypatch.setattr(worker, "arq_settings", lambda: ArqSettings(job_completion_wait=42))
    await _built()

    assert worker_env.latest().kwargs["job_completion_wait"] == 42


def test_from_args_rejects_an_unknown_option() -> None:
    with pytest.raises(click.UsageError):
        ArqWorkerRuntime.from_args(["--nonsense"])


def test_reading_the_worker_before_build_is_a_loud_error() -> None:
    with pytest.raises(RuntimeError, match=r"build\(\) has not run"):
        _ = ArqWorkerRuntime.from_args([]).worker


# -- running and stopping ------------------------------------------------------------


async def test_run_on_loop_runs_the_worker(worker_env) -> None:
    runtime = await _built()

    await runtime.run_on_loop()

    assert worker_env.latest().ran is True


async def test_run_on_loop_reports_a_cancellation_and_propagates_it(worker_env, monkeypatch, caplog) -> None:
    """Cancellation must leave the caller with a cancelled task -- not one that
    quietly completed."""

    class _IdleWorker(_FakeWorker):
        async def async_run(self) -> None:
            self.ran = True
            await asyncio.Event().wait()

    monkeypatch.setattr(worker, "Worker", _IdleWorker)
    runtime = await _built()
    task = asyncio.create_task(runtime.run_on_loop())
    await _running(worker_env)

    with caplog.at_level("INFO", logger="tai42_backend_arq.worker"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    assert any("Worker cancelled" in record.message for record in caplog.records)


async def test_request_drain_asks_arq_for_a_warm_shutdown_once(worker_env) -> None:
    """arq schedules a fresh wait-then-cancel task per call, so a second request
    would open a second completion window and cut the first one short. The host
    is entitled to ask twice (on the signal, then on the cancellation path)."""
    runtime = await _built()

    runtime.request_drain()
    runtime.request_drain()

    assert worker_env.latest().warm == [signal.SIGTERM]
    assert worker_env.latest().cold == []


async def test_request_terminate_escalates_a_drain_to_a_cold_stop(worker_env) -> None:
    runtime = await _built()

    runtime.request_drain()
    runtime.request_terminate()

    assert worker_env.latest().warm == [signal.SIGTERM]
    assert worker_env.latest().cold == [signal.SIGTERM]


# -- teardown ------------------------------------------------------------------------


async def test_aclose_closes_the_shared_pool_and_the_worker(worker_env, pool_close) -> None:
    runtime = await _built()

    await runtime.aclose()

    pool_close.assert_awaited_once()
    assert worker_env.latest().closed is True


async def test_aclose_without_a_built_worker_still_closes_the_shared_pool(worker_env, pool_close) -> None:
    # The launch path closes on every exit, including one where ``build`` raised.
    await ArqWorkerRuntime.from_args([]).aclose()

    pool_close.assert_awaited_once()
    assert worker_env.instances == []


async def test_aclose_after_a_completed_drain_does_not_cancel_finished_work(pool_close) -> None:
    """``Worker.close`` calls ``handle_sig(SIGUSR1)`` ITSELF when the worker was
    built with ``handle_signals=False`` (arq's embedded-worker path), and then
    gathers whatever is left in ``tasks``. Work the drain already finished must
    survive that untouched -- otherwise closing after a clean drain would report
    the drained jobs as cancelled.

    Runs against a REAL :class:`arq.Worker`: the hazard is arq's own code.
    """
    runtime = await _built()
    built = runtime.worker
    pool = _StubPool()
    # The pool a real run would have created inside ``Worker.main``; without it
    # ``close`` returns before the gather this test is about.
    cast(Any, built)._pool = pool

    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    built.tasks["job-1"] = finished
    built.main_task = asyncio.create_task(asyncio.sleep(0))
    await built.main_task

    await runtime.aclose()

    assert not finished.cancelled()
    assert finished.exception() is None
    assert pool.closed is True
    pool_close.assert_awaited_once()


async def test_aclose_severs_work_that_outlived_the_drain(pool_close) -> None:
    """The other face of the same arq behavior, pinned so it is not a surprise:
    ``close`` cancels whatever is STILL running and re-raises that cancellation
    out of its own gather.

    Only reachable after a drain that overran its budget — by then the base has
    already reported the overrun at ERROR and teardown is the cold stop — but it
    means closing a wedged worker ends in a ``CancelledError`` rather than a
    quiet close.
    """
    runtime = await _built()
    built = runtime.worker
    cast(Any, built)._pool = _StubPool()

    in_flight = asyncio.create_task(asyncio.sleep(30))
    await asyncio.sleep(0)
    built.tasks["job-1"] = in_flight

    with pytest.raises(asyncio.CancelledError):
        await runtime.aclose()

    assert in_flight.cancelled()


# -- the standalone entry point -------------------------------------------------------


def test_main_runs_a_standalone_worker_with_arq_owning_the_signals(worker_env, pool_close) -> None:
    """The direct CLI has no host chain to compose with and no app booting
    alongside it, so arq keeps its own handlers -- the only thing that would
    drain a SIGTERM there."""
    result = CliRunner().invoke(worker.main, ["--max-jobs", "3"], catch_exceptions=False)

    assert result.exit_code == 0
    built = worker_env.latest()
    assert built.kwargs["max_jobs"] == 3
    assert built.kwargs["handle_signals"] is True
    assert built.ran is True
    assert built.closed is True


def test_main_keyboard_interrupt_exits_130(monkeypatch) -> None:
    monkeypatch.setattr(worker, "_run_standalone", AsyncMock(side_effect=KeyboardInterrupt))

    result = CliRunner().invoke(worker.main, [])

    assert result.exit_code == 130


# -- ArqBackend.launch -----------------------------------------------------------------


async def test_launch_without_a_subcommand_names_the_ones_it_has() -> None:
    with pytest.raises(ValueError, match="arq launch requires a subcommand: worker"):
        await ArqBackend().launch([])


async def test_launch_rejects_an_unknown_subcommand() -> None:
    with pytest.raises(ValueError, match="Unknown arq launch subcommand 'beat'"):
        await ArqBackend().launch(["beat"])


async def test_launch_rejects_an_unknown_worker_option(worker_env) -> None:
    with pytest.raises(click.UsageError):
        await ArqBackend().launch(["worker", "--nonsense"])

    assert worker_env.instances == []


async def test_launch_worker_parses_its_options_and_runs_the_worker(worker_env, pool_close) -> None:
    await ArqBackend().launch(["worker", "--max-jobs", "7", "--burst"])

    built = worker_env.latest()
    assert built.kwargs["max_jobs"] == 7
    assert built.kwargs["burst"] is True
    assert built.kwargs["job_timeout"] == 300
    assert built.ran is True
    assert built.closed is True
    pool_close.assert_awaited_once()


async def test_launch_does_not_consume_until_the_app_is_boot_ready(stub_app, worker_env, pool_close) -> None:
    """Building is not consuming, so the worker object may exist early; RUNNING
    it against a half-built tool registry would fail every job permanently."""
    stub_app.lifecycle.ready.clear()
    task = asyncio.create_task(ArqBackend().launch(["worker"]))
    try:
        for _ in range(3):
            await asyncio.sleep(0.01)
            assert not task.done()
            assert worker_env.latest().ran is False

        stub_app.lifecycle.ready.set()
        await asyncio.wait_for(task, timeout=5)
        assert worker_env.latest().ran is True
    finally:
        if not task.done():
            task.cancel()


async def test_launch_refuses_to_consume_when_the_app_never_becomes_ready(
    stub_app, worker_env, pool_close, monkeypatch
) -> None:
    monkeypatch.setattr(ArqBackend, "ready_timeout", 0.05)
    stub_app.lifecycle.ready.clear()

    with pytest.raises(RuntimeError, match=r"arq worker: the app did not become ready within 0\.05s"):
        await ArqBackend().launch(["worker"])

    assert worker_env.latest().ran is False
    # Teardown runs on the refusal path too.
    assert worker_env.latest().closed is True
    pool_close.assert_awaited_once()


async def test_launch_cancellation_lets_the_arq_drain_finish_before_unwinding(worker_env, pool_close, monkeypatch):
    """The sensitive path for this backend: arq's drain is itself a task on the
    serving loop, and the cancellation that requests it arrives on the same
    signal. If the base did not shield the run body, that cancellation would
    starve the drain and sever exactly the jobs it was meant to let finish."""

    class _DrainingWorker(_FakeWorker):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.job_completed = False
            self._stopped = asyncio.Event()
            self._drain: asyncio.Task[None] | None = None

        async def async_run(self) -> None:
            self.ran = True
            await self._stopped.wait()

        def handle_sig_wait_for_completion(self, signum: signal.Signals) -> None:
            super().handle_sig_wait_for_completion(signum)
            # arq's own shape: the warm handler schedules the wait as a task and
            # returns at once.
            self._drain = asyncio.get_running_loop().create_task(self._finish())

        async def _finish(self) -> None:
            await asyncio.sleep(0)
            self.job_completed = True
            self._stopped.set()

    monkeypatch.setattr(worker, "Worker", _DrainingWorker)
    task = asyncio.create_task(ArqBackend().launch(["worker"]))
    built = await _running(worker_env)
    assert isinstance(built, _DrainingWorker)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert built.warm == [signal.SIGTERM]
    assert built.job_completed is True
    assert built.closed is True


def test_the_drain_budget_tracks_the_live_job_completion_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """``job_completion_wait`` is how long arq lets in-flight jobs finish, so the
    host's drain wait has to outlast it — read LIVE, not frozen at import."""
    from tai42_kit.settings import reset_all_settings

    from tai42_backend_arq.settings import arq_settings

    instance = ArqBackend()
    assert instance.drain_timeout == float(arq_settings().job_completion_wait) + 30.0

    monkeypatch.setenv("ARQ_JOB_COMPLETION_WAIT", "600")
    reset_all_settings()
    try:
        assert instance.drain_timeout == 630.0
    finally:
        monkeypatch.delenv("ARQ_JOB_COMPLETION_WAIT", raising=False)
        reset_all_settings()


async def test_build_takes_the_queue_name_from_settings(worker_env, monkeypatch) -> None:
    """An unset ``--queue-name`` binds the worker's consume target to the
    ``ARQ_QUEUE_NAME`` setting, so it tracks the enqueue pool's default queue
    without a CLI flag — the seam per-stack isolation rides on."""
    from tai42_backend_arq.settings import ArqSettings

    monkeypatch.setattr(worker, "arq_settings", lambda: ArqSettings(queue_name="tai42_e2e_abc123:arq:queue"))
    await _built()

    assert worker_env.latest().kwargs["queue_name"] == "tai42_e2e_abc123:arq:queue"


async def test_explicit_queue_name_flag_beats_the_setting(worker_env, monkeypatch) -> None:
    from tai42_backend_arq.settings import ArqSettings

    monkeypatch.setattr(worker, "arq_settings", lambda: ArqSettings(queue_name="from-setting"))
    await _built("--queue-name", "from-flag")

    assert worker_env.latest().kwargs["queue_name"] == "from-flag"
