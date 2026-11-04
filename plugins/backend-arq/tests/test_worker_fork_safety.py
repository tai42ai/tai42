"""The arq worker's fork safety: the kit's fork guard over every fork its process makes.

The worker forks no child of its own, but code it runs does (a tool that starts a
``multiprocessing`` child, a ``subprocess`` with a ``preexec_fn``). The worker build installs
the kit's fork safety, so each such child starts with a reset copy of the fork gate and evicts
the monitoring writer it inherited, and a fork off the loop waits out a running re-import
without reporting a pass of the poll loop, which runs on the event loop.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from tai42_kit.backend import fork_safety
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

from tai42_backend_arq import worker
from tai42_backend_arq.worker import ArqWorkerRuntime

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")


class _BuiltWorker:
    """Stands in for arq's worker: the build constructs it and nothing runs it."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


async def _built(monkeypatch: pytest.MonkeyPatch, reports: list[float]) -> ArqWorkerRuntime:
    """A worker runtime built as the launch path builds it, its progress reports recorded."""
    monkeypatch.setattr(worker, "_ProgressReportingWorker", _BuiltWorker)
    runtime = ArqWorkerRuntime.from_args([])
    runtime.bind_progress(lambda *, next_within: reports.append(next_within))
    await runtime.build()
    return runtime


def _fork_reporting(read: Callable[[], Any]) -> Any:
    """Fork a child that reports ``read()`` back to the parent, and return what it reported."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os.write(write_end, json.dumps(read()).encode())
        os._exit(0)
    os.close(write_end)
    try:
        reported = os.read(read_end, 4096)
    finally:
        os.close(read_end)
        os.waitpid(pid, 0)
    return json.loads(reported)


async def test_a_fork_in_an_arq_worker_resets_the_child_s_gate_and_evicts_its_monitoring(monkeypatch, stub_app):
    """A child forked in the worker process starts with no inherited job span and a fresh monitoring writer."""
    await _built(monkeypatch, [])
    span_open = threading.Event()
    release = threading.Event()

    def hold_a_job_span() -> None:
        with fork_gate.job_span(timeout=5.0):
            span_open.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold_a_job_span, daemon=True)
    holder.start()
    assert span_open.wait(5.0)
    try:
        child = _fork_reporting(
            lambda: {
                "live_spans": fork_gate.live_spans,
                "shutdowns": stub_app.monitoring.active.writer.shutdown_calls,
            }
        )
    finally:
        release.set()
        holder.join(5.0)

    assert child == {"live_spans": 0, "shutdowns": stub_app.monitoring.active.writer.shutdown_calls + 1}


async def test_a_fork_off_the_loop_waits_out_a_running_reimport_reporting_no_pass_of_the_poll_loop(monkeypatch):
    """A fork on a thread with no loop of its own waits for the re-import; it reports nothing for the poll loop."""
    reports: list[float] = []
    await _built(monkeypatch, reports)
    guard = fork_gate._fork_hooks.guard
    assert guard is not None
    assert guard.timeout == JOB_SPAN_WAIT_SECONDS
    assert guard.after_in_child is fork_safety.evict_inherited_monitoring
    held = threading.Event()
    released_at: list[float] = []
    forked_at: list[float] = []

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=5.0):
            held.set()
            time.sleep(1.5)
            released_at.append(time.monotonic())

    def fork() -> None:
        forked_at.append(_fork_reporting(time.monotonic))

    rebuilder = threading.Thread(target=rebuild, daemon=True)
    rebuilder.start()
    assert held.wait(5.0)
    forker = threading.Thread(target=fork, daemon=True)
    forker.start()
    # Blocks this test's loop, the one arq's poll loop runs on, for the whole wait: no pass happens.
    forker.join(5.0)
    rebuilder.join(5.0)

    assert len(forked_at) == 1
    assert forked_at[0] >= released_at[0]
    assert reports == []
    assert fork_gate.live_spans == 0
