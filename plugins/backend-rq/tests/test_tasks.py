"""The job functions and the enqueue option mapping."""

from __future__ import annotations

from typing import Any

import pytest
from rq.exceptions import NoSuchJobError
from tai42_contract.interactions import SuspendedInteraction
from tai42_kit.backend import CallbackSchema
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_SUBJECT_ARG,
)
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_rq import tasks
from tai42_backend_rq.settings import rq_settings

from .conftest import make_client_ctx

_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}
_FORWARDED = {
    SCHEDULE_SUBJECT_ARG: _SUBJECT,
    SCHEDULE_EXECUTION_KEY_ARG: "svc",
    SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
}

# The secret-read capability the enqueue path carries onto every callback spec.
_CARRIED = {WORKER_SECRET_CAPABILITY_ARG: False}


# --- tool_execution ----------------------------------------------------------


async def test_tool_execution_dispatches_the_tool(app):
    app.tools.run_result = {"out": 1}

    arg_name = rq_settings().tool_name_arg
    result = await tasks.tool_execution(**{arg_name: "my_tool", WORKER_SECRET_CAPABILITY_ARG: False, "x": 5})

    assert result == {"out": 1}
    assert app.tools.run_calls == [("my_tool", {"x": 5})]


def test_enqueue_serializer_refuses_secret_value_payload():
    """RQ writes a job through its default (pickle) serializer; a ``SecretValue``
    in the job args must raise loudly there, never pickle the real value into
    the result/job store. The message carries no secret text."""
    from rq.serializers import resolve_serializer
    from tai42_contract.secrets import SecretValue

    serializer = resolve_serializer(None)
    with pytest.raises(TypeError) as excinfo:
        serializer.dumps({"args": (SecretValue("tok-4242-xyzzy"),), "kwargs": {}})
    assert "tok-4242-xyzzy" not in str(excinfo.value)


async def test_tool_execution_raises_the_tool_s_error(app):
    app.tools.run_error = RuntimeError("tool blew up")

    arg_name = rq_settings().tool_name_arg
    with pytest.raises(RuntimeError, match="tool blew up"):
        await tasks.tool_execution(**{arg_name: "my_tool", WORKER_SECRET_CAPABILITY_ARG: False})


# --- callback_job ------------------------------------------------------------


class FakeResult:
    """Result-shaped: its str() is a repr, the failure text lives in exc_string."""

    def __init__(self, exc_string: str | None) -> None:
        if exc_string is not None:
            self.exc_string = exc_string

    def __repr__(self) -> str:
        return "Result(id=r-1, type=FAILED)"


class FakeFetchedJob:
    def __init__(
        self,
        *,
        failed: bool = False,
        finished: bool = True,
        value: Any = None,
        exc_string: str | None = "job failed hard",
    ) -> None:
        self.is_failed = failed
        self.is_finished = finished
        self._value = value
        self._exc_string = exc_string

    def latest_result(self) -> FakeResult:
        return FakeResult(self._exc_string)

    def return_value(self) -> Any:
        return self._value

    def get_status(self) -> str:
        return "started"


def _patch_job_fetch(monkeypatch, job: FakeFetchedJob | Exception) -> None:
    class FakeJobClass:
        @staticmethod
        def fetch(job_id: str, connection: Any = None) -> FakeFetchedJob:
            if isinstance(job, Exception):
                raise job
            return job

    monkeypatch.setattr(tasks, "Job", FakeJobClass)
    monkeypatch.setattr(tasks, "client_ctx", make_client_ctx(object()))


async def test_callback_job_runs_callback_on_success(monkeypatch):
    _patch_job_fetch(monkeypatch, FakeFetchedJob(value={"n": 3}))
    seen: list[tuple[Any, Any]] = []

    async def fake_callback_execution(result: Any, callback: Any, *, settings: Any) -> str:
        seen.append((result, callback))
        return "chained"

    monkeypatch.setattr(tasks, "callback_execution", fake_callback_execution)

    callback = CallbackSchema(tool="next_tool", carried_kwargs=_CARRIED)
    assert await tasks.callback_job("job-1", callback) == "chained"
    assert seen == [({"n": 3}, callback)]


async def test_callback_job_raises_on_a_failed_primary_with_exc_string(monkeypatch):
    """The raised error carries the Result's persisted traceback text, not the
    Result object's repr."""
    _patch_job_fetch(monkeypatch, FakeFetchedJob(failed=True))
    with pytest.raises(RuntimeError, match=r"^callback predecessor job job-1 failed: job failed hard$"):
        await tasks.callback_job("job-1", CallbackSchema(tool="t", carried_kwargs=_CARRIED))


async def test_callback_job_failed_primary_without_exc_string_names_the_result(monkeypatch):
    _patch_job_fetch(monkeypatch, FakeFetchedJob(failed=True, exc_string=None))
    with pytest.raises(RuntimeError, match=r"failed: Result\(id=r-1, type=FAILED\)$"):
        await tasks.callback_job("job-1", CallbackSchema(tool="t", carried_kwargs=_CARRIED))


async def test_callback_job_raises_on_an_unfinished_primary(monkeypatch):
    _patch_job_fetch(monkeypatch, FakeFetchedJob(finished=False))
    with pytest.raises(RuntimeError, match=r"^callback predecessor job job-1 is still started$"):
        await tasks.callback_job("job-1", CallbackSchema(tool="t", carried_kwargs=_CARRIED))


async def test_callback_job_missing_primary_raises(monkeypatch):
    _patch_job_fetch(monkeypatch, NoSuchJobError("gone"))
    with pytest.raises(NoSuchJobError):
        await tasks.callback_job("job-1", CallbackSchema(tool="t", carried_kwargs=_CARRIED))


# --- the callback runs under the followed run's door context ---------------


async def test_callback_job_that_asks_parks_under_the_forwarded_identity(app, monkeypatch):
    # A dequeued callback whose tool asks parks under the carried identity + subject and hands back the
    # re-park sentinel — the ask is never returned as the callback's value.
    _patch_job_fetch(monkeypatch, FakeFetchedJob(value={"value": 3}))
    sentinel = SuspendedInteraction(interaction_id="i-1", caller_interaction_ids=["i-1"])
    app.interactions.park_sentinel = sentinel

    out = await tasks.callback_job("job-1", CallbackSchema(tool="follow", carried_kwargs={**_FORWARDED, **_CARRIED}))

    assert out is sentinel
    assert app.interactions.binds == [("svc", "fp-1")]
    assert app.interactions.visit_calls[0].receives_outcome is False
    assert app.interactions.visit_calls[0].context.door == "schedule"
