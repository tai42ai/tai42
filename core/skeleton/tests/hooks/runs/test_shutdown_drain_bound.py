"""The hook-run stop's declared shutdown drain: twice the grace bounds a healthy stop.

An attempt that finishes within its grace, and one that settles its cancel within the second
grace, both end inside :func:`shutdown_drain_seconds` with no ERROR logged. Only an attempt that
ignores its cancel reaches the third wait, and the stop reports it at ERROR before that wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.sweep import shutdown_drain_seconds, stop_hook_runs_sweep

_GRACE = 0.2


@pytest.fixture
def attempts(monkeypatch: pytest.MonkeyPatch) -> dict[str, asyncio.Task[None]]:
    """A process running stand-in attempts under a short shutdown grace."""
    registry: dict[str, asyncio.Task[None]] = {}
    manager = SimpleNamespace(settings=SimpleNamespace(run_shutdown_grace_seconds=_GRACE))
    monkeypatch.setattr(runtime, "_ATTEMPTS", registry)
    monkeypatch.setattr(runtime, "_STOPPING", False)
    monkeypatch.setattr(runtime, "manager", lambda: manager)
    return registry


def _spawn(registry: dict[str, asyncio.Task[None]], name: str, body) -> asyncio.Task[None]:
    task = asyncio.create_task(body(), name=name)
    registry[name] = task
    return task


async def _finishes_within_its_grace() -> None:
    await asyncio.sleep(_GRACE / 2)


async def _settles_its_cancel_within_the_second_grace() -> None:
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        # The interrupted-attempt rule's terminal write, inside the second grace.
        await asyncio.sleep(_GRACE / 2)
        raise


async def _ignores_the_stops_two_cancels() -> None:
    for _ in range(2):
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(3600)
    await asyncio.sleep(3600)


def test_the_declared_drain_is_twice_the_grace(attempts: dict[str, asyncio.Task[None]]) -> None:
    assert shutdown_drain_seconds() == 2 * _GRACE


async def test_a_healthy_stop_ends_within_the_declared_drain_with_no_error(
    attempts: dict[str, asyncio.Task[None]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    _spawn(attempts, "finishes", _finishes_within_its_grace)
    _spawn(attempts, "settles", _settles_its_cancel_within_the_second_grace)
    loop = asyncio.get_running_loop()

    started = loop.time()
    await stop_hook_runs_sweep()
    elapsed = loop.time() - started

    assert elapsed < shutdown_drain_seconds()
    assert all(task.done() for task in attempts.values())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_an_attempt_ignoring_its_cancel_is_reported_before_the_third_wait(
    attempts: dict[str, asyncio.Task[None]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    wedged = _spawn(attempts, "wedged", _ignores_the_stops_two_cancels)
    loop = asyncio.get_running_loop()
    first_error_at: list[float] = []

    class _Stamp(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR and not first_error_at:
                first_error_at.append(loop.time())

    stamp = _Stamp()
    logging.getLogger("tai42_skeleton.hooks.runs.sweep").addHandler(stamp)
    try:
        started = loop.time()
        await stop_hook_runs_sweep()
    finally:
        logging.getLogger("tai42_skeleton.hooks.runs.sweep").removeHandler(stamp)
        # The stand-in honours a third cancel.
        wedged.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wedged

    # The first ERROR lands once the declared drain is spent, before the third grace begins.
    [first] = first_error_at
    assert shutdown_drain_seconds() <= first - started < shutdown_drain_seconds() + _GRACE
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2
