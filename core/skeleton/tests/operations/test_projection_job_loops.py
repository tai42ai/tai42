"""The projected tool edge runs an operation's post-reply task before the call returns, on any loop.

A tool door that runs inside a backend job drives the projected tool on the job runner's loop: a
fresh loop run once for the job, or a persistent loop run once per job. Neither loop is stepped
again after the job's coroutine finishes, so the post-reply task (the writer's graceful exit)
must have run by the time the projected call returned. A failing task is logged at ERROR and
never raised into the reply: the operation's change already landed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from starlette.background import BackgroundTask
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.operations import OperationRegistry, OperationResponse, operation
from tai42_skeleton.operations.projection import project_operations


class _Tools:
    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(self, *, force, name, tags, annotations):  # type: ignore[no-untyped-def]
        def decorator(func):  # type: ignore[no-untyped-def]
            self.registered[name] = func
            return func

        return decorator


def _projected_op_arming(after_reply: Callable[[], None]) -> Callable[..., Any]:
    """Project one operation whose result arms ``after_reply`` as its post-reply task; return the tool."""
    reg = OperationRegistry()

    @operation(name="arming_op", summary="s", tags=["t"], registry=reg)
    async def arming_op() -> OperationResponse:
        return OperationResponse({"status": "ok"}, BackgroundTask(after_reply))

    tools = _Tools()

    class _App:
        pass

    app = _App()
    app.tools = tools  # type: ignore[attr-defined]
    project_operations(app, ApiToolsConfig(include=["arming_op"]), registry=reg)
    return tools.registered["arming_op"]


def test_the_post_reply_task_ran_before_a_fresh_job_loop_run_once_returned() -> None:
    ran: list[str] = []
    tool = _projected_op_arming(lambda: ran.append("exit"))
    loop = asyncio.new_event_loop()
    try:
        reply = loop.run_until_complete(tool())
        assert reply == {"status": "ok"}
        assert ran == ["exit"]
    finally:
        loop.close()


def test_the_post_reply_task_ran_before_a_persistent_job_loop_finished_the_job() -> None:
    ran: list[str] = []
    tool = _projected_op_arming(lambda: ran.append("exit"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(tool())
        # Nothing else runs on the loop until the next job: the task already ran.
        assert ran == ["exit"]
        # The next job on the same loop runs only its own task; nothing was left pending.
        loop.run_until_complete(tool())
        assert ran == ["exit", "exit"]
    finally:
        loop.close()


async def test_a_failing_post_reply_task_is_logged_at_error_and_the_reply_still_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _fails() -> None:
        raise RuntimeError("signal refused")

    tool = _projected_op_arming(_fails)

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations.projection"):
        reply = await tool()

    assert reply == {"status": "ok"}
    errors = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.name == "tai42_skeleton.operations.projection"
    ]
    assert len(errors) == 1
    assert "post-reply task failed" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert isinstance(errors[0].exc_info[1], RuntimeError)
