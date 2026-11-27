"""``running_supervisor``: the enrolled supervisor a run's code belongs to, and what a snapshot of its context keeps."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import importlib
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

from tai42_skeleton.app import epoch as epoch_module


def _supervisor() -> Any:
    """The CURRENT supervisor module: the operations conftest re-imports operation modules per test."""
    return importlib.import_module("tai42_skeleton.operations.tool_runs.supervisor")


@pytest.fixture(autouse=True)
def _admitting_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every enrolled run here is admitted by epoch 7."""
    monkeypatch.setattr(epoch_module, "current_epoch_or_none", lambda: SimpleNamespace(number=7))


async def test_a_finished_supervisor_is_not_kept_by_a_snapshot_of_its_context() -> None:
    supervisor = _supervisor()
    snapshots: list[contextvars.Context] = []
    seen_inside: list[bool] = []

    async def run() -> None:
        found = supervisor.running_supervisor()
        seen_inside.append(found is not None and found[0] is asyncio.current_task())
        snapshots.append(contextvars.copy_context())

    task = asyncio.create_task(run())
    supervisor._enroll_supervisor(task)
    task.add_done_callback(supervisor._discard_supervisor)
    await task
    finished = weakref.ref(task)
    del task
    await asyncio.sleep(0)
    gc.collect()

    assert seen_inside == [True]
    assert snapshots
    assert finished() is None
    assert snapshots[0].run(supervisor.running_supervisor) is None


async def test_the_supervisor_is_found_in_a_task_its_run_spawned() -> None:
    supervisor = _supervisor()
    found: list[Any] = []

    async def child() -> None:
        found.append(supervisor.running_supervisor())

    async def run() -> None:
        await asyncio.create_task(child())

    task = asyncio.create_task(run())
    supervisor._enroll_supervisor(task)
    task.add_done_callback(supervisor._discard_supervisor)
    await task

    assert found == [(task, 7)]
    assert supervisor.running_supervisor() is None
