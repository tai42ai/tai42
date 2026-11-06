"""A dispatched job's run: detached (no live caller), bounded (the job budget) and admitted, only inside the scope.

The job body runs the job's tool inside that scope, under the secret-read capability the job carried and the
backend's job budget (``task_timeout``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.access_control.context import caller_may_read_secrets
from tai42_contract.app import tai42_app

from tai42_kit.backend.job_run import dispatched_job, run_dispatched_tool
from tai42_kit.backend.settings import BackendDispatchSettings
from tai42_kit.utils.detached_util import in_detached_run
from tai42_kit.utils.run_bound import run_bound
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG


class _AdmissionLog:
    """The app's job-admission door, noting every entry and exit and the marks set at entry."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.marks_at_entry: list[tuple[bool, bool]] = []

    @contextmanager
    def admitted_job(self) -> Iterator[None]:
        self.events.append("enter")
        self.marks_at_entry.append((in_detached_run(), run_bound() is not None))
        try:
            yield
        finally:
            self.events.append("exit")


@pytest.fixture
def admissions() -> Iterator[_AdmissionLog]:
    log = _AdmissionLog()
    with tai42_app.bound(SimpleNamespace(lifecycle=log)):
        yield log


async def test_a_dispatched_job_is_detached_bounded_by_its_budget_and_admitted(admissions: _AdmissionLog) -> None:
    assert (in_detached_run(), run_bound()) == (False, None)
    async with dispatched_job(budget=120):
        bound = run_bound()
        assert bound is not None
        assert 119 < bound.seconds_left() <= 120
        assert in_detached_run() is True
        assert admissions.events == ["enter"]
    assert (in_detached_run(), run_bound()) == (False, None)
    assert admissions.events == ["enter", "exit"]
    assert admissions.marks_at_entry == [(True, True)]


async def test_the_work_a_job_owes_once_it_answered_runs_after_its_admission_ended(admissions: _AdmissionLog) -> None:
    seen: list[tuple[list[str], bool]] = []

    async def owed() -> None:
        seen.append((list(admissions.events), in_detached_run()))

    async with dispatched_job(budget=120):
        bound = run_bound()
        assert bound is not None
        bound.once_answered(owed)

    assert seen == [(["enter", "exit"], True)]


async def test_every_mark_is_reset_and_the_admission_ended_when_the_job_raises(admissions: _AdmissionLog) -> None:
    with pytest.raises(RuntimeError):
        async with dispatched_job(budget=120):
            raise RuntimeError("boom")
    assert (in_detached_run(), run_bound()) == (False, None)
    assert admissions.events == ["enter", "exit"]


class _ToolRuns:
    """The app's tool runner, noting each run and the marks, admission and capability it ran under."""

    def __init__(self, admissions: _AdmissionLog) -> None:
        self.admissions = admissions
        self.runs: list[tuple[str, dict[str, Any]]] = []
        self.seen: list[tuple[bool, float | None, list[str], bool]] = []

    async def run_tool(self, name: str, kwargs: dict[str, Any], *, offload_sync: bool) -> str:
        assert offload_sync is True
        self.runs.append((name, dict(kwargs)))
        bound = run_bound()
        seconds = None if bound is None else round(bound.seconds_left())
        self.seen.append((in_detached_run(), seconds, list(self.admissions.events), caller_may_read_secrets()))
        return f"ran {name}"


@pytest.fixture
def tool_runs() -> Iterator[_ToolRuns]:
    log = _AdmissionLog()
    runs = _ToolRuns(log)
    with tai42_app.bound(SimpleNamespace(lifecycle=log, tools=runs)):
        yield runs


_TOOL_NAME_ARG = "job_tool"
_SETTINGS = BackendDispatchSettings(tool_name_arg=_TOOL_NAME_ARG, task_timeout=90)


@pytest.mark.parametrize("capability", [True, False])
async def test_the_job_body_runs_the_named_tool_in_the_dispatched_scope(tool_runs: _ToolRuns, capability: bool) -> None:
    kwargs = {_TOOL_NAME_ARG: "echo", WORKER_SECRET_CAPABILITY_ARG: capability, "text": "hi"}
    assert await run_dispatched_tool(kwargs, settings=_SETTINGS) == "ran echo"
    assert tool_runs.runs == [("echo", {"text": "hi"})]
    # The tool ran under the settings' job budget.
    assert tool_runs.seen == [(True, 90, ["enter"], capability)]
    assert tool_runs.admissions.events == ["enter", "exit"]
    assert (in_detached_run(), run_bound(), caller_may_read_secrets()) == (False, None, False)


@pytest.mark.parametrize("missing", [_TOOL_NAME_ARG, WORKER_SECRET_CAPABILITY_ARG])
async def test_a_job_without_its_tool_name_or_capability_raises_before_any_run(
    tool_runs: _ToolRuns, missing: str
) -> None:
    kwargs: dict[str, Any] = {_TOOL_NAME_ARG: "echo", WORKER_SECRET_CAPABILITY_ARG: False, "text": "hi"}
    del kwargs[missing]
    with pytest.raises(KeyError, match=missing):
        await run_dispatched_tool(kwargs, settings=_SETTINGS)
    assert tool_runs.runs == []
    assert tool_runs.admissions.events == []
