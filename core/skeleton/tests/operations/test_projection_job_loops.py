"""The projected tool edge runs the writer's exit a call's change owes before the call returns, on any loop.

A tool door that runs inside a backend job drives the projected tool on the job runner's loop: a
fresh loop run once for the job, or a persistent loop run once per job. Neither loop is stepped
again after the job's coroutine finishes, so the writer's graceful exit the call's change owes
must have run by the time the projected call returned. A failing exit is logged at ERROR and
never raised into the reply: the operation's change already landed. A call that fails after its
change's roll armed the exit runs it all the same before the error raises. An operation declaring
``holds_writer_exit`` holds the worker's exit from before it runs, and on a worker that decided to
leave raises the refusal's text as the call's tool error before it runs; one declaring no hold is
not held.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import WorkerBus
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, open_writer_exit_scope
from tai42_skeleton.operations import OperationRegistry, operation
from tai42_skeleton.operations.projection import project_operations

from .._fakes.bus import FakeBus


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> FakeBus:
    """This process's bus member, the writer whose exit a projected call's change owes."""
    fake = FakeBus(origin="backend-writer", remotes=[])
    monkeypatch.setattr(instance.app, "_bus", fake)
    return fake


def _owe_writer_exit(change: str) -> None:
    """Stand in for an armed roll: hold the writer's exit and owe it into the open scope."""
    bus = cast("FakeBus", instance.app.bus)
    scope = open_writer_exit_scope(change)
    scope.hold_exit(cast("WorkerBus", bus), change)
    scope.owe(cast("WorkerBus", bus))


class _Tools:
    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(self, *, force, name, tags, annotations):  # type: ignore[no-untyped-def]
        def decorator(func):  # type: ignore[no-untyped-def]
            self.registered[name] = func
            return func

        return decorator


def _project(reg: OperationRegistry, name: str) -> Callable[..., Any]:
    tools = _Tools()

    class _App:
        pass

    app = _App()
    app.tools = tools  # type: ignore[attr-defined]
    project_operations(app, ApiToolsConfig(include=[name]), registry=reg)
    return tools.registered[name]


def _projected_op_arming() -> Callable[..., Any]:
    """Project one operation whose change's roll armed the writer's exit; return the tool."""
    reg = OperationRegistry()

    @operation(name="arming_op", summary="s", tags=["t"], registry=reg)
    async def arming_op() -> dict[str, str]:
        _owe_writer_exit("the arming change")
        return {"status": "ok"}

    return _project(reg, "arming_op")


def test_the_writer_exit_ran_before_a_fresh_job_loop_run_once_returned(bus: FakeBus) -> None:
    tool = _projected_op_arming()
    loop = asyncio.new_event_loop()
    try:
        reply = loop.run_until_complete(tool())
        assert reply == {"status": "ok"}
        assert bus.exit_requests == [WRITER_EXIT_REASON]
        assert bus.exit_deferred is None
    finally:
        loop.close()


def test_the_writer_exit_ran_before_a_persistent_job_loop_finished_the_job(bus: FakeBus) -> None:
    from fastmcp.exceptions import ToolError

    tool = _projected_op_arming()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(tool())
        # Nothing else runs on the loop until the next job: the exit already ran.
        assert bus.exit_requests == [WRITER_EXIT_REASON]
        # The next job steps the same loop again: the worker is leaving, so that job's change is
        # refused before it begins, and nothing was left pending to run.
        with pytest.raises(ToolError, match=r"^leaving — "):
            loop.run_until_complete(tool())
        assert bus.exit_requests == [WRITER_EXIT_REASON]
        assert bus.exit_deferred is None
    finally:
        loop.close()


async def test_a_failing_writer_exit_is_logged_at_error_and_the_reply_still_returns(
    bus: FakeBus, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bus, "mark_recycling", AsyncMock(side_effect=RuntimeError("presence write refused")))
    tool = _projected_op_arming()

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations.projection"):
        reply = await tool()

    assert reply == {"status": "ok"}
    errors = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.name == "tai42_skeleton.operations.projection"
    ]
    assert len(errors) == 1
    assert "after a projected call failed" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert isinstance(errors[0].exc_info[1], RuntimeError)
    # The failed exit released the change's hold: the worker is not left holding it for good.
    assert bus.exit_deferred is None


async def test_a_failed_call_runs_the_writer_exit_its_change_owes_before_the_error_raises(bus: FakeBus) -> None:
    from fastmcp.exceptions import ToolError

    from tai42_skeleton.operations.errors import OperationFailedError

    reg = OperationRegistry()

    @operation(name="failing_armed_op", summary="s", tags=["t"], registry=reg, errors=[OperationFailedError])
    async def failing_armed_op() -> dict[str, str]:
        _owe_writer_exit("the failed change")
        raise OperationFailedError("the change landed; its roll converged")

    with pytest.raises(ToolError, match="the change landed; its roll converged"):
        await _project(reg, "failing_armed_op")()

    assert bus.exit_requests == [WRITER_EXIT_REASON]


def _projected_recording(
    reg: OperationRegistry, ran: list[str | None], *, holds_writer_exit: bool
) -> Callable[..., Any]:
    """Project one operation that records the exit held while it runs; return the tool."""

    @operation(
        name="changing_op", summary="Change the thing", tags=["t"], registry=reg, holds_writer_exit=holds_writer_exit
    )
    async def changing_op() -> dict[str, str]:
        ran.append(cast("FakeBus", instance.app.bus).exit_deferred)
        return {"status": "ok"}

    return _project(reg, "changing_op")


async def test_a_projected_operation_holding_the_writer_exit_on_a_leaving_worker_raises_before_it_runs(
    bus: FakeBus,
) -> None:
    from fastmcp.exceptions import ToolError

    ran: list[str | None] = []
    tool = _projected_recording(OperationRegistry(), ran, holds_writer_exit=True)
    assert bus.decide_departure("a fleet recycle op") is None

    with pytest.raises(ToolError) as refused:
        await tool()

    assert str(refused.value) == (
        "leaving — this worker is leaving (a fleet recycle op) and begins no change; retry shortly"
    )
    assert ran == []
    assert bus.exit_deferred is None


async def test_a_projected_operation_holding_the_writer_exit_holds_it_from_before_it_runs(bus: FakeBus) -> None:
    ran: list[str | None] = []
    tool = _projected_recording(OperationRegistry(), ran, holds_writer_exit=True)

    assert await tool() == {"status": "ok"}

    assert ran == ["Change the thing"]
    assert bus.exit_deferred is None


async def test_a_projected_operation_declaring_no_hold_is_not_held(bus: FakeBus) -> None:
    ran: list[str | None] = []
    tool = _projected_recording(OperationRegistry(), ran, holds_writer_exit=False)
    assert bus.decide_departure("a fleet recycle op") is None

    assert await tool() == {"status": "ok"}

    assert ran == [None]
    assert bus.exit_deferred is None


async def test_a_call_raising_a_broadcast_error_whose_roll_armed_the_writer_exit_runs_it_before_raising(
    bus: FakeBus,
) -> None:
    # A config change whose propagation failed after its roll converged with the writer needing
    # its own restart: the exit runs on the tool edge too, and the error still raises.
    from tai42_skeleton.app.bus.models import FleetResult
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    reg = OperationRegistry()

    @operation(name="failing_reload_op", summary="s", tags=["t"], registry=reg)
    async def failing_reload_op() -> dict[str, str]:
        _owe_writer_exit("the failed reload")
        raise FleetBroadcastError("reload_config", FleetResult(op="reload_config"), RuntimeError("boom"))

    with pytest.raises(FleetBroadcastError, match="boom"):
        await _project(reg, "failing_reload_op")()

    assert bus.exit_requests == [WRITER_EXIT_REASON]


async def test_a_cancelled_call_abandons_the_exit_and_releases_its_hold(
    bus: FakeBus, caplog: pytest.LogCaptureFixture
) -> None:
    reg = OperationRegistry()
    started = asyncio.Event()

    @operation(name="stalled_op", summary="s", tags=["t"], registry=reg)
    async def stalled_op() -> dict[str, str]:
        _owe_writer_exit("the stalled change")
        started.set()
        await asyncio.Event().wait()
        return {"status": "ok"}

    call = asyncio.create_task(_project(reg, "stalled_op")())
    await started.wait()
    with caplog.at_level(logging.ERROR):
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    assert bus.exit_requests == []
    assert bus.exit_deferred is None
    assert any("owes this writer's own restart" in record.message for record in caplog.records)
