"""``tool_execution`` / ``callback_job`` worker functions and the callback glue."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.jobs import JobStatus
from tai42_contract.access_control import caller_may_read_secrets
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.template import TemplatedText
from tai42_kit.backend import CallbackSchema, callback_execution, prepare_backend_kwargs
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import JqBudgetExceededError
from tai42_kit.utils.data.jq_util import run_jq_first
from tai42_kit.utils.detached_util import in_detached_run
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_SUBJECT_ARG,
)
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_arq import tasks
from tai42_backend_arq.settings import ArqSettings, TaskFailedError, arq_settings

from .conftest import handle_of

_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}
_FORWARDED = {
    SCHEDULE_SUBJECT_ARG: _SUBJECT,
    SCHEDULE_EXECUTION_KEY_ARG: "svc",
    SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
}

# The secret-read capability the enqueue path carries onto every callback spec.
_CARRIED = {WORKER_SECRET_CAPABILITY_ARG: False}


class _Ctx:
    def __init__(self) -> None:
        self.enqueued: list[tuple[Any, ...]] = []

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.enqueued.append(args)
        return None


# -- tool_execution --------------------------------------------------------------------


async def test_tool_execution_runs_named_tool(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value={"out": 1})
    ctx = {"redis": _Ctx(), "job_id": "job-9"}

    out = await tasks.tool_execution(ctx, backend_tool_name="mytool", backend_secret_capability=False, text="hi")

    assert out == {"out": 1}
    stub_app.tools.run_tool_mock.assert_awaited_once_with("mytool", {"text": "hi"})


async def test_tool_execution_chains_the_callback_after_the_tool_succeeds(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value={"out": 1})
    redis = _Ctx()
    ctx = {"redis": redis, "job_id": "job-9"}

    await tasks.tool_execution(
        ctx, backend_tool_name="mytool", backend_secret_capability=False, callback_kwargs={"tool": "next"}
    )

    assert redis.enqueued == [("callback_job", "job-9", {"tool": "next"})]


async def test_tool_execution_chains_no_callback_when_the_tool_fails(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(side_effect=RuntimeError("tool blew up"))
    redis = _Ctx()
    ctx = {"redis": redis, "job_id": "job-9"}

    with pytest.raises(RuntimeError, match="tool blew up"):
        await tasks.tool_execution(
            ctx, backend_tool_name="mytool", backend_secret_capability=False, callback_kwargs={"tool": "next"}
        )

    assert redis.enqueued == []


# -- callback_job ------------------------------------------------------------------------


class _FakeJob:
    def __init__(self, *, statuses: list[Any], result: Any) -> None:
        self._statuses = list(statuses)
        self._result = result

    async def status(self) -> Any:
        value = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        if isinstance(value, Exception):
            raise value
        return value

    async def result(self, timeout: float | None = None) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _bind_job(monkeypatch, job: _FakeJob, callback_timeout: int = 5) -> None:
    monkeypatch.setattr(tasks, "job_handle", handle_of(lambda *a, **kw: job))
    monkeypatch.setattr(tasks, "arq_settings", lambda: ArqSettings(callback_timeout=callback_timeout))


async def test_callback_job_runs_callback_over_result(monkeypatch, stub_app) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result={"value": 3}))
    stub_app.tools.run_tool_mock = AsyncMock(return_value="chained")

    out = await tasks.callback_job(
        {"redis": object()},
        "job-1",
        {"tool": "next_tool", "expr": {"content": "{v: .value}"}, "carried_kwargs": dict(_CARRIED)},
    )

    assert out == "chained"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next_tool", {"v": 3})


async def test_callback_job_raises_on_a_missing_predecessor(monkeypatch) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.not_found], result=None))
    with pytest.raises(RuntimeError, match=r"^callback predecessor job job-1 not found$"):
        await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())


async def test_callback_job_raises_on_a_predecessor_unfinished_at_the_timeout(monkeypatch) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.in_progress], result=None), callback_timeout=0)
    with pytest.raises(TimeoutError, match=r"^callback predecessor job job-1 still in_progress after 0 seconds$"):
        await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())


async def test_callback_job_propagates_unexpected_poll_error(monkeypatch) -> None:
    # An unexpected polling error (a Redis fault reading the predecessor's status)
    # is not a domain outcome: it propagates so arq marks the callback job failed.
    _bind_job(monkeypatch, _FakeJob(statuses=[ConnectionError("redis gone")], result=None))
    with pytest.raises(ConnectionError, match="redis gone"):
        await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())


async def test_callback_job_raises_the_failed_predecessors_failure(monkeypatch) -> None:
    _bind_job(
        monkeypatch,
        _FakeJob(statuses=[JobStatus.complete], result=TaskFailedError("ValueError", "ValueError('job failed')", None)),
    )
    with pytest.raises(TaskFailedError, match="job failed"):
        await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())


async def test_callback_job_raises_when_callback_execution_fails(monkeypatch, stub_app) -> None:
    # The predecessor succeeds, but the callback's own execution fails: that
    # follow-up delivery failure raises into arq's failed-job machinery rather
    # than being swallowed into a status dict.
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result={"value": 3}))
    monkeypatch.setattr(tasks, "callback_execution", AsyncMock(side_effect=RuntimeError("callback blew up")))
    with pytest.raises(RuntimeError, match="callback blew up"):
        await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema(tool="next", carried_kwargs=_CARRIED))


async def test_callback_job_raises_an_aborted_predecessor_as_a_failure(monkeypatch) -> None:
    """An aborted predecessor replays its stored abort as a revived ``CancelledError``; the
    callback job raises it as ``TaskFailedError`` with the stored detail instead of letting it
    read as a cancellation of the callback job itself."""
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result=asyncio.CancelledError("CancelledError()")))
    with pytest.raises(TaskFailedError, match=r"CancelledError\(\)"):
        await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())


# -- callback_execution -------------------------------------------------------------------


async def test_callback_condition_pass_runs_tool(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value="ran")
    cb = CallbackSchema(
        condition=TemplatedText(content=".ok"),
        expr=TemplatedText(content="{x: .value}"),
        tool="next",
        carried_kwargs=_CARRIED,
    )

    out = await callback_execution({"ok": True, "value": 5}, cb, settings=arq_settings())

    assert out == "ran"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next", {"x": 5})


async def test_callback_runs_tool_detached(stub_app) -> None:
    # A worker execution has no live caller, so the callback's tool observes the
    # detached flag set; the flag never leaks past the callback.
    cb = CallbackSchema(
        condition=TemplatedText(content=".ok"),
        expr=TemplatedText(content="{x: .value}"),
        tool="next",
        carried_kwargs=_CARRIED,
    )

    await callback_execution({"ok": True, "value": 5}, cb, settings=arq_settings())

    assert stub_app.tools.detached_seen == [True]
    assert stub_app.tools.offloads == [True]
    assert in_detached_run() is False


@pytest.mark.parametrize("capability", [True, False])
async def test_callback_binds_the_carried_capability(stub_app, capability: bool) -> None:
    # A dequeued callback's follow-up tool sees the capability the enqueue path carried, reset after.
    cb = CallbackSchema(
        condition=TemplatedText(content=".ok"),
        expr=TemplatedText(content="{x: .value}"),
        tool="next",
        carried_kwargs={WORKER_SECRET_CAPABILITY_ARG: capability},
    )

    await callback_execution({"ok": True, "value": 5}, cb, settings=arq_settings())

    assert stub_app.tools.secret_capability_seen == [capability]
    assert caller_may_read_secrets() is False


async def test_callback_condition_fail_returns_none(stub_app) -> None:
    cb = CallbackSchema(
        condition=TemplatedText(content=".ok"),
        expr=TemplatedText(content="{x: .value}"),
        tool="next",
        carried_kwargs=_CARRIED,
    )
    out = await callback_execution({"ok": False, "value": 5}, cb, settings=arq_settings())
    assert out is None
    stub_app.tools.run_tool_mock.assert_not_called()


async def test_callback_condition_empty_pipeline_skips(stub_app) -> None:
    # A condition that evaluates to an EMPTY pipeline (emits nothing) must skip the
    # callback (return None) rather than crash with the opaque RuntimeError.
    cb = CallbackSchema(
        condition=TemplatedText(content=".errors[] | select(.fatal)"),
        expr=TemplatedText(content="{x: .value}"),
        tool="next",
        carried_kwargs=_CARRIED,
    )
    out = await callback_execution({"errors": [{"fatal": False}], "value": 5}, cb, settings=arq_settings())
    assert out is None
    stub_app.tools.run_tool_mock.assert_not_called()


async def test_callback_expr_empty_pipeline_yields_empty_mapping(stub_app) -> None:
    # An expr that evaluates to an EMPTY pipeline yields {} (default), passed to the
    # tool as {} — never the opaque RuntimeError.
    stub_app.tools.run_tool_mock = AsyncMock(return_value="ran")
    cb = CallbackSchema(
        condition=TemplatedText(content=".ok"),
        expr=TemplatedText(content=".errors[] | select(.fatal)"),
        tool="next",
        carried_kwargs=_CARRIED,
    )
    out = await callback_execution({"ok": True, "errors": [{"fatal": False}]}, cb, settings=arq_settings())
    assert out == "ran"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next", {})


async def test_callback_without_tool_returns_expr_output() -> None:
    cb = CallbackSchema(expr=TemplatedText(content="{doubled: (.value * 2)}"))
    out = await callback_execution({"value": 4}, cb, settings=arq_settings())
    assert out == {"doubled": 8}


async def test_callback_without_expr_yields_empty_kwargs(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value="ran")
    cb = CallbackSchema(tool="next", carried_kwargs=_CARRIED)
    out = await callback_execution({"value": 4}, cb, settings=arq_settings())
    assert out == "ran"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next", {})


async def test_callback_jq_eval_is_timeout_bounded(stub_app, monkeypatch) -> None:
    # The callback path evaluates jq through ``run_jq_first``, so a runaway condition is killed in
    # its worker at JQ_TIMEOUT_SECONDS and JqBudgetExceededError, naming that setting, is raised.
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.2")
    reset_all_settings()
    try:
        # A first evaluation starts the compute pool, so the timed call measures the budget alone.
        assert await run_jq_first(".", 1) == 1
        cb = CallbackSchema(
            condition=TemplatedText(content="last(range(1e18))"),
            expr=TemplatedText(content="{x: .value}"),
            tool="next",
            carried_kwargs=_CARRIED,
        )
        start = time.monotonic()
        with pytest.raises(JqBudgetExceededError, match="JQ_TIMEOUT_SECONDS"):
            await callback_execution({"ok": True, "value": 5}, cb, settings=arq_settings())
        assert time.monotonic() - start < 1.0
    finally:
        reset_all_settings()


# -- prepare_backend_kwargs / render methods -----------------------------------------------


async def test_prepare_backend_kwargs_injects_tool_name_and_stamps_capability() -> None:
    async def tool(a: int) -> int:
        return a

    # No request is bound in the test, so ``caller_may_read_secrets`` reads the fail-closed
    # default and the stamped capability is False; the worker pops it before running the tool.
    out = await prepare_backend_kwargs(tool, "backend_tool_name", "tool", {"a": 1})
    assert out == {"a": 1, "backend_tool_name": "tool", "backend_secret_capability": False}


async def test_rendered_fields_resolve_through_resource_manager() -> None:
    cb = CallbackSchema(condition=TemplatedText(content=".ok"), expr=TemplatedText(content=".x"))
    assert await cb.rendered_condition() == ".ok"
    assert await cb.rendered_expr() == ".x"
    empty = CallbackSchema()
    assert await empty.rendered_condition() == ""
    assert await empty.rendered_expr() == ""


async def test_rendered_condition_resolves_template_id(stub_app) -> None:
    # A by-id condition resolves through the resource manager's stored template map.
    stub_app.storage.resource_manager.templates["cond-1"] = ".ok"
    cb = CallbackSchema(condition=TemplatedText(id="cond-1"))
    assert await cb.rendered_condition() == ".ok"


async def test_rendered_condition_unknown_id_raises(stub_app) -> None:
    # A by-id condition whose id resolves to nothing raises loudly instead of
    # silently rendering an empty condition.
    cb = CallbackSchema(condition=TemplatedText(id="missing"))
    with pytest.raises(KeyError):
        await cb.rendered_condition()


# -- the callback carries the followed run's door context --------------------------------


async def test_callback_job_that_asks_parks_under_the_forwarded_identity(monkeypatch, stub_app) -> None:
    # A dequeued callback whose tool asks parks under the carried identity + subject and hands back the
    # re-park sentinel — the ask is never returned as the callback's value.
    sentinel = SuspendedInteraction(interaction_id="i-1", caller_interaction_ids=["i-1"])
    stub_app.interactions.park_sentinel = sentinel
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result={"value": 3}))
    cb = CallbackSchema(tool="follow", carried_kwargs={**_FORWARDED, **_CARRIED})

    out = await tasks.callback_job({"redis": object()}, "job-1", cb)

    assert out is sentinel
    assert stub_app.interactions.binds == [("svc", "fp-1")]
    assert stub_app.interactions.visit_calls[0].receives_outcome is False
    assert stub_app.interactions.visit_calls[0].context.door == "schedule"
