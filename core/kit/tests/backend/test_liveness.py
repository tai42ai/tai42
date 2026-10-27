"""The template binds a liveness file to every runtime that declares ``ProgressLiveness``.

The file is the runtime's progress reporter for the span of ``launch``: bound before
``build``, written from the runtime's own loop, removed on every way out. A runtime
declaring ``HttpLiveness`` is probed over HTTP and gets no file. The wedge proof is
the point of the signal: a process whose loop stops passing turns stale while the
process itself is still alive.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from tai42_contract.backend.runtime import BackendRuntime, ProgressLiveness

from tai42_kit.backend import ManagedBackend
from tai42_kit.liveness import LivenessFile, backend_liveness_settings, check_liveness

from .fakes import (
    FakeApp,
    FakeForkingBackend,
    FakeOnLoopWorker,
    FakeSchedulerRuntime,
    FakeThreadWorker,
    RecordingRuntime,
    running_runtime,
)

_Runtimes = dict[str, type[BackendRuntime]]

# The promise each fake pass makes, and the grace the checks below allow past it.
_NEXT_WITHIN = 0.05
_GRACE = 0.2
_PASS_SECONDS = 0.01


class _ReportingInline(FakeSchedulerRuntime):
    """A non-consuming loop runtime (a scheduler's beat): it reports, reads its own file, and returns."""

    liveness = ProgressLiveness()
    seen: ClassVar[str | None] = None
    built_with_reporter: ClassVar[bool] = False

    async def build(self) -> None:
        await super().build()
        self.report_progress(next_within=_NEXT_WITHIN)
        type(self).built_with_reporter = True

    def run_blocking(self) -> None:
        self._enter_body()
        self.report_progress(next_within=_NEXT_WITHIN)
        type(self).seen = backend_liveness_settings().path.read_text()
        self._leave_body()


class _UnboundHttpServer(FakeSchedulerRuntime):
    """An HTTP-probed runtime that reports anyway: no reporter is bound to it."""

    def run_blocking(self) -> None:
        self._enter_body()
        self.report_progress(next_within=_NEXT_WITHIN)


class _WedgingOnLoopWorker(FakeOnLoopWorker):
    """A consuming on-loop worker that reports for ``passes`` passes, then stops passing but stays running."""

    passes: ClassVar[int] = 3

    async def run_on_loop(self) -> None:
        self._enter_body()
        for _ in range(self.passes):
            self.report_progress(next_within=_NEXT_WITHIN)
            await asyncio.sleep(_PASS_SECONDS)
        self.wedged.set()
        await self._stopped.wait()
        self._leave_body()

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self.wedged = threading.Event()


class _WedgingThreadWorker(FakeThreadWorker):
    """The worker-thread shape of :class:`_WedgingOnLoopWorker`."""

    passes: ClassVar[int] = 3

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self.wedged = threading.Event()

    def run_blocking(self) -> None:
        self._enter_body()
        for _ in range(self.passes):
            self.report_progress(next_within=_NEXT_WITHIN)
            time.sleep(_PASS_SECONDS)
        self.wedged.set()
        self._stopped.wait()
        self._leave_body()


class _OnLoopBackend(ManagedBackend):
    label = "liveness-on-loop"
    runtimes: ClassVar[_Runtimes] = {
        "worker": _WedgingOnLoopWorker,
        "scheduler": _ReportingInline,
        "server": _UnboundHttpServer,
    }


class _ThreadBackend(FakeForkingBackend):
    label = "liveness-thread"
    runtimes: ClassVar[_Runtimes] = {"worker": _WedgingThreadWorker}


@pytest.fixture(autouse=True)
def _fast_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh margin as short as the fakes' promise, so a stall shows within a fraction of a second."""
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_INTERVAL", str(_NEXT_WITHIN))
    for runtime_cls in (_ReportingInline, _UnboundHttpServer, _WedgingOnLoopWorker, _WedgingThreadWorker):
        runtime_cls.latest = None
    _ReportingInline.seen = None
    _ReportingInline.built_with_reporter = False


async def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.01)


async def test_a_progress_runtime_is_bound_before_build_and_its_file_is_removed_after(liveness_path: Path) -> None:
    await _OnLoopBackend().launch(["scheduler"])

    assert _ReportingInline.built_with_reporter is True
    assert _ReportingInline.seen is not None
    pid, horizon = _ReportingInline.seen.splitlines()
    assert int(pid) > 0
    assert float(horizon) == pytest.approx(2 * _NEXT_WITHIN)
    assert not liveness_path.exists()


async def test_an_http_runtime_gets_no_file_and_cannot_report(liveness_path: Path) -> None:
    with pytest.raises(RuntimeError, match="_UnboundHttpServer reported progress with no reporter bound"):
        await _OnLoopBackend().launch(["server"])

    assert not liveness_path.exists()


@pytest.mark.parametrize(
    ("backend_cls", "worker_cls"),
    [(_OnLoopBackend, _WedgingOnLoopWorker), (_ThreadBackend, _WedgingThreadWorker)],
    ids=["on_loop", "worker_thread"],
)
async def test_a_loop_that_stops_passing_turns_stale_while_the_process_lives(
    app: FakeApp, liveness_path: Path, backend_cls: type[ManagedBackend], worker_cls: type[RecordingRuntime]
) -> None:
    task = asyncio.create_task(backend_cls().launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(worker_cls)
    assert isinstance(runtime, _WedgingOnLoopWorker | _WedgingThreadWorker)

    await _until(lambda: liveness_path.exists())
    assert check_liveness(liveness_path, _GRACE).alive
    await asyncio.to_thread(runtime.wedged.wait, 5.0)

    await _until(lambda: check_liveness(liveness_path, _GRACE).state == "stale")
    assert not task.done(), "the launch ended: the stall must show while the process still runs"

    runtime.release()
    await asyncio.wait_for(task, timeout=5)
    assert not liveness_path.exists()


async def test_a_loop_that_keeps_passing_stays_alive(app: FakeApp, liveness_path: Path) -> None:
    """The no-false-positive guard: a loop passing on its promise is never reported stale."""
    _WedgingThreadWorker.passes = 60
    try:
        task = asyncio.create_task(_ThreadBackend().launch(["worker"]))
        app.lifecycle.mark_ready()
        runtime = await running_runtime(_WedgingThreadWorker)
        assert isinstance(runtime, _WedgingThreadWorker)
        await _until(lambda: liveness_path.exists())

        while not runtime.wedged.is_set():
            assert check_liveness(liveness_path, _GRACE).alive
            await asyncio.sleep(_PASS_SECONDS)

        runtime.release()
        await asyncio.wait_for(task, timeout=5)
    finally:
        _WedgingThreadWorker.passes = 3


async def test_the_file_is_removed_when_a_consuming_body_raises(app: FakeApp, liveness_path: Path) -> None:
    class _FailingWorker(_WedgingThreadWorker):
        def run_blocking(self) -> None:
            self.report_progress(next_within=_NEXT_WITHIN)
            assert liveness_path.exists()
            raise RuntimeError("engine failed")

    class _Backend(_ThreadBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _FailingWorker}

    app.lifecycle.mark_ready()
    with pytest.raises(RuntimeError, match="engine failed"):
        await _Backend().launch(["worker"])

    assert not liveness_path.exists()


async def test_the_file_is_removed_when_a_non_consuming_body_raises(liveness_path: Path) -> None:
    class _FailingScheduler(_ReportingInline):
        def run_blocking(self) -> None:
            self.report_progress(next_within=_NEXT_WITHIN)
            assert liveness_path.exists()
            raise RuntimeError("beat failed")

    class _Backend(_OnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"scheduler": _FailingScheduler}

    with pytest.raises(RuntimeError, match="beat failed"):
        await _Backend().launch(["scheduler"])

    assert not liveness_path.exists()


async def test_the_file_is_removed_when_the_clean_release_raises(liveness_path: Path) -> None:
    class _FailingClose(_ReportingInline):
        async def aclose(self) -> None:
            raise OSError("engine close failed")

    class _Backend(_OnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"scheduler": _FailingClose}

    with pytest.raises(OSError, match="engine close failed"):
        await _Backend().launch(["scheduler"])

    assert not liveness_path.exists()


async def test_a_removal_failure_on_the_clean_path_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(self: LivenessFile) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(LivenessFile, "remove", _refuse)

    with pytest.raises(OSError, match="read-only file system"):
        await _OnLoopBackend().launch(["scheduler"])


async def test_a_removal_failure_while_unwinding_is_logged_and_the_cause_survives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, liveness_path: Path
) -> None:
    class _FailingScheduler(_ReportingInline):
        def run_blocking(self) -> None:
            raise RuntimeError("beat failed")

    class _Backend(_OnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"scheduler": _FailingScheduler}

    def _refuse(self: LivenessFile) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(LivenessFile, "remove", _refuse)

    with (
        caplog.at_level(logging.ERROR, logger="tai42_kit.backend.base"),
        pytest.raises(RuntimeError, match="beat failed"),
    ):
        await _Backend().launch(["scheduler"])

    assert [record.getMessage() for record in caplog.records] == [
        f"liveness-on-loop scheduler: removing the liveness file {liveness_path} failed while the launch was "
        "already unwinding"
    ]
