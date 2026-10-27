"""An optional argument the caller left out never ships to the worker.

A branch over a synthesized signature receives every optional parameter the caller left out as
the omitted-value marker. The job kwargs and the schedule entry hold only what the caller
supplied, so they encode with the task serializer and the worker runs the named tool with its
own default.
"""

from __future__ import annotations

from typing import Any

import pytest
from kombu.utils.json import dumps, loads
from tai42_kit.utils.schedule_subject import schedule_create_fire
from tai42_kit.utils.unset import UNSET

import tai42_backend_celery.extensions.extensions as extensions
from tai42_backend_celery.core.tasks import tool_execution

from .test_extensions import _INTERVAL_30, _FakeAsyncResult, _FakeEntry, _FakeTask


async def probe(text: str, count: Any = UNSET) -> str:
    """A tool whose optional argument defaults to the omitted-value marker."""
    return f"{text}:{count}"


_SHIPPED = {"text": "x", "backend_tool_name": "probe", "backend_secret_capability": False}


@pytest.fixture
def fake_task(monkeypatch: pytest.MonkeyPatch) -> _FakeTask:
    task = _FakeTask(_FakeAsyncResult(value="worker-result"))
    monkeypatch.setattr(extensions, "tool_execution", task)
    return task


@pytest.fixture
def fake_entry(monkeypatch: pytest.MonkeyPatch) -> type[_FakeEntry]:
    _FakeEntry.instances = []
    monkeypatch.setattr(extensions, "RedBeatSchedulerEntry", _FakeEntry)
    return _FakeEntry


@pytest.mark.parametrize("factory", [extensions.sync_task, extensions.async_task])
async def test_a_dispatched_job_carries_only_the_supplied_arguments(fake_task: _FakeTask, factory: Any) -> None:
    branch = factory(probe, "probe", "doc")
    await branch(text="x")
    (call,) = fake_task.calls
    assert loads(dumps(call["kwargs"])) == _SHIPPED


async def test_a_schedule_entry_carries_only_the_supplied_arguments(fake_entry: type[_FakeEntry]) -> None:
    branch = extensions.schedule_task(probe, "probe", "doc")
    with schedule_create_fire():
        await branch(text="x", backend_schedule_name="nightly", backend_schedule=_INTERVAL_30)
    (entry,) = fake_entry.instances
    assert loads(dumps(entry.kwargs["kwargs"])) == _SHIPPED


def test_the_worker_runs_the_named_tool_without_the_omitted_argument(stub_app: Any) -> None:
    stub_app.tools.run_tool_result = "ran"
    try:
        out = tool_execution.apply(kwargs=loads(dumps(_SHIPPED))).get()
    finally:
        tool_execution.close_loop()
    assert out == "ran"
    assert stub_app.tools.run_tool_calls == [("probe", {"text": "x"})]
