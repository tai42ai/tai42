"""The worker runtime builder, its CLI, and ``ArqBackend.launch`` parsing."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import click
import pytest
from click.testing import CliRunner

from tai42_backend_arq import worker
from tai42_backend_arq.backend import ArqBackend
from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.scheduler import recover_stalled_schedules


class _FakeWorker:
    captured: ClassVar[dict[str, Any]] = {}
    closed = False

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured = kwargs
        type(self).closed = False

    async def async_run(self) -> None:
        # A burst worker's run returns when the queue drains; model that so
        # ``start_arq_worker`` completes and tears its resources down.
        return None

    async def close(self) -> None:
        type(self).closed = True


@pytest.fixture
def worker_env(monkeypatch) -> type[_FakeWorker]:
    monkeypatch.setattr(worker, "Worker", _FakeWorker)
    monkeypatch.setattr(RedisPoolManager, "close", AsyncMock())
    return _FakeWorker


async def test_start_arq_worker_builds_worker_and_cleans_up(worker_env) -> None:
    await worker.start_arq_worker(None, False, 3600, "arq:queue", 10, 300, 0.5, 5, 60)

    captured = worker_env.captured
    assert captured["allow_abort_jobs"] is True
    assert captured["on_startup"] is recover_stalled_schedules
    assert len(captured["functions"]) == 3
    assert captured["keep_result"] == 3600
    assert captured["max_jobs"] == 10
    assert worker_env.closed is True


async def test_start_arq_worker_zero_keep_result_passes_through(worker_env) -> None:
    # arq itself treats keep_result=0 as "keep nothing"; it must not be
    # rewritten (arq crashes on a None keep_result at job completion).
    await worker.start_arq_worker(None, True, 0, "arq:queue", 10, 300, 0.5, 5, 60)
    assert worker_env.captured["keep_result"] == 0
    assert worker_env.captured["burst"] is True


async def test_start_arq_worker_outer_cancellation_shuts_down_and_propagates(worker_env, monkeypatch, caplog) -> None:
    """Cancellation shuts both tasks down and then propagates, so the caller
    sees a cancelled task -- not one that quietly completed."""

    class _IdleWorker(_FakeWorker):
        async def async_run(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(worker, "Worker", _IdleWorker)
    task = asyncio.create_task(worker.start_arq_worker(None, False, 3600, "arq:queue", 10, 300, 0.5, 5, 60))
    await asyncio.sleep(0.01)

    with caplog.at_level("INFO", logger="tai42_backend_arq.worker"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    assert any("Worker cancelled" in record.message for record in caplog.records)
    assert _IdleWorker.closed is True


async def test_start_arq_worker_redis_url_override_applies(worker_env) -> None:
    await worker.start_arq_worker("redis://elsewhere:6390/3", False, 3600, "arq:queue", 10, 300, 0.5, 5, 60)

    redis_settings = worker_env.captured["redis_settings"]
    assert redis_settings.host == "elsewhere"
    assert redis_settings.port == 6390
    assert redis_settings.database == 3


def test_main_runs_worker(monkeypatch) -> None:
    started = AsyncMock()
    monkeypatch.setattr(worker, "start_arq_worker", started)
    result = CliRunner().invoke(worker.main, ["--max-jobs", "3"])
    assert result.exit_code == 0
    assert started.await_args is not None
    assert started.await_args.args[4] == 3  # max_jobs


def test_main_keyboard_interrupt_exits_130(monkeypatch) -> None:
    monkeypatch.setattr(worker, "start_arq_worker", AsyncMock(side_effect=KeyboardInterrupt))
    result = CliRunner().invoke(worker.main, [])
    assert result.exit_code == 130


# -- ArqBackend.launch ----------------------------------------------------------------


async def test_launch_no_args_exits() -> None:
    with pytest.raises(SystemExit):
        await ArqBackend().launch([])


async def test_launch_unknown_subcommand_exits() -> None:
    with pytest.raises(SystemExit):
        await ArqBackend().launch(["beat"])


async def test_launch_worker_parses_args(monkeypatch) -> None:
    started = AsyncMock()
    monkeypatch.setattr(worker, "start_arq_worker", started)

    await ArqBackend().launch(["worker", "--max-jobs", "7", "--burst"])

    assert started.await_args is not None
    kwargs = started.await_args.kwargs
    assert kwargs["max_jobs"] == 7
    assert kwargs["burst"] is True
    assert kwargs["job_timeout"] == 300


async def test_launch_rejects_unknown_option(monkeypatch) -> None:
    monkeypatch.setattr(worker, "start_arq_worker", AsyncMock())
    with pytest.raises(click.UsageError):
        await ArqBackend().launch(["worker", "--nonsense"])
