"""A drain marks the supervisors it cancels, so ``run_recorded`` tells a drain apart from any other cancel.

A run whose supervisor a drain cancelled raises :class:`ToolRunDrainedError` with the drain's
reason; a tool that raises its own ``CancelledError``, or an awaiting task that is itself
cancelled, re-raises a plain ``CancelledError``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.authz import execution as execution_module
from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

from .._fakes.tool_runs_redis import FakeRedis


def _ops():
    """The CURRENT tool-run package: the operations conftest re-imports operation modules per test."""
    return importlib.import_module("tai42_skeleton.operations.tool_runs")


def _supervisor():
    return importlib.import_module("tai42_skeleton.operations.tool_runs.supervisor")


class _Tools:
    """A tool runner whose one tool runs the scripted ``body``."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.body = self._block

    async def _block(self) -> None:
        await asyncio.Event().wait()

    async def get_tool(self, name):
        return SimpleNamespace(name=name, meta=None)

    async def run_tool(self, name, tool_input, *, offload_sync=False, extras=None):
        self.started.set()
        await self.body()
        return {"ok": True}


class _Interactions:
    async def normalise_started(self, value):
        return SimpleNamespace(kind="result", result=value, action="started")


@pytest.fixture
def tools(monkeypatch) -> Iterator[_Tools]:
    ops = _ops()
    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=False))
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeRedis()
    settings = ToolRunsSettings()

    @asynccontextmanager
    async def ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake

    monkeypatch.setattr(ops, "client_ctx", ctx)
    monkeypatch.setattr(ops, "tool_runs_settings", lambda: settings)
    monkeypatch.setattr(ops, "_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    runner = _Tools()
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=runner, interactions=_Interactions()))
    yield runner
    for task in list(ops._SUPERVISORS):
        task.cancel()


async def _recorded() -> None:
    async with bind_execution_identity("k-run", bound_fingerprint="fp-run"):
        await _ops().run_recorded("probe", {}, crash_resume_eligible=False)


async def test_a_drained_supervisor_raises_the_drain_with_its_reason(tools) -> None:
    run = asyncio.create_task(_recorded())
    await tools.started.wait()
    await _supervisor().drain_supervisors(reason="r")

    with pytest.raises(_supervisor().ToolRunDrainedError) as raised:
        await run
    assert raised.value.reason == "r"
    assert isinstance(raised.value, asyncio.CancelledError)


async def _own_cancel() -> None:
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    await future


async def test_a_tool_s_own_cancel_re_raises_a_plain_cancel(tools) -> None:
    tools.body = _own_cancel

    with pytest.raises(asyncio.CancelledError) as raised:
        await _recorded()
    assert not isinstance(raised.value, _supervisor().ToolRunDrainedError)


async def test_a_cancelled_awaiting_task_re_raises_a_plain_cancel_after_the_terminal_write(tools) -> None:
    run = asyncio.create_task(_recorded())
    await tools.started.wait()
    run.cancel()

    with pytest.raises(asyncio.CancelledError) as raised:
        await run
    assert not isinstance(raised.value, _supervisor().ToolRunDrainedError)
    assert run.cancelled()
    assert not [task for task in _ops()._SUPERVISORS if not task.done()]


async def test_a_supervisor_an_epoch_drain_skipped_carries_no_mark_until_its_own_epoch_drains(
    tools, monkeypatch
) -> None:
    from tai42_skeleton.app import epoch as epoch_module

    monkeypatch.setattr(epoch_module, "current_epoch_or_none", lambda: SimpleNamespace(number=3))
    run = asyncio.create_task(_recorded())
    await tools.started.wait()
    [enrolled] = [task for task in _ops()._SUPERVISORS if not task.done()]

    await _supervisor().drain_supervisors(epoch=4, reason="another generation retires")

    assert not enrolled.done()
    assert enrolled not in _supervisor()._DRAINED

    await _supervisor().drain_supervisors(epoch=3, reason="its own generation retires")
    with pytest.raises(_supervisor().ToolRunDrainedError) as raised:
        await run
    assert raised.value.reason == "its own generation retires"


async def test_a_drained_crash_resume_replay_ends_cancelled_and_logs_no_error(tools, caplog) -> None:
    caplog.set_level(logging.ERROR)
    importlib.import_module("tai42_skeleton.operations.tool_runs.reconcile")._spawn_crash_resume(
        "r-x", {"tool_name": "probe", "arguments": "{}", "extras": "{}"}
    )
    [replay] = [task for task in _ops()._SUPERVISORS if task.get_name() == "tai-crash-resume-r-x"]
    await tools.started.wait()

    await _supervisor().drain_supervisors(reason="the server is shutting down")
    await asyncio.gather(replay, return_exceptions=True)

    assert replay.cancelled()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and "r-x" in r.getMessage()]
