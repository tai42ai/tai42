"""The three branch tools a backend registers over its task door and its schedule writer."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastmcp.tools import Tool
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.backend import (
    SCHEDULE_CADENCE_KEY,
    SCHEDULE_EXTENSION,
    SCHEDULE_NAME_KEY,
    CallbackSchema,
    IntervalCadence,
    TaskWaitTimeoutError,
)
from tai42_contract.extensions import ExtensionKind
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY

from tai42_kit.backend import ASYNC_TASK_EXTENSION, BACKEND_TASK_OPTS, SYNC_TASK_EXTENSION, register_task_branches
from tai42_kit.backend.job_context import running_job
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_CONTRACT_ARG,
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_STAMPED_DOOR_OPTS,
    ReservedScheduleKeyError,
    schedule_create_fire,
)
from tai42_kit.utils.unset import UNSET
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from .branch_fakes import BranchApp, BranchSettings, FakeScheduleWriter, FakeTaskDoor

_HOURLY = {"kind": "interval", "every": 3600, "relative": False}
_TOOL_NAME_ARG = BranchSettings().tool_name_arg


async def sample_tool(x: int, note: str = "hi", count: Any = UNSET) -> dict[str, Any]:
    """Sample tool the branches are minted over."""
    return {"x": x, "note": note}


def plain_tool(a: int, b: str = "x") -> str:
    """A tool whose every default is plain JSON, as a listed schema needs."""
    return f"{a}-{b}"


@pytest.fixture
def app() -> Iterator[BranchApp]:
    fake = BranchApp()
    with tai42_app.bound(fake):
        yield fake


@pytest.fixture(autouse=True)
def _gate(gate_declared: None) -> None:
    """Every submit carries the gate state onto a callback; this suite declares it."""


@pytest.fixture
def door(app: BranchApp) -> FakeTaskDoor:
    return FakeTaskDoor(result={"ran": True}, lifecycle=app.lifecycle)


@pytest.fixture
def writer() -> FakeScheduleWriter:
    return FakeScheduleWriter()


@pytest.fixture
def branch(app: BranchApp, door: FakeTaskDoor, writer: FakeScheduleWriter) -> Callable[..., Callable[..., Any]]:
    """Register the branches over the fakes; return a minter of the named branch over ``sample_tool``."""
    register_task_branches(door, writer, BranchSettings, "acme")

    def mint(extension: str, tool: Callable[..., Any] = sample_tool) -> Callable[..., Any]:
        _kind, factory = app.extensions.factories[extension]
        return factory(tool, tool.__name__, "the doc")

    return mint


def test_the_three_branches_register_as_backend_extensions(app: BranchApp, door: FakeTaskDoor) -> None:
    register_task_branches(door, FakeScheduleWriter(), BranchSettings, "acme")
    assert {name: kind for name, (kind, _f) in app.extensions.factories.items()} == {
        SYNC_TASK_EXTENSION: ExtensionKind.BACKEND,
        ASYNC_TASK_EXTENSION: ExtensionKind.BACKEND,
        SCHEDULE_EXTENSION: ExtensionKind.BACKEND,
    }


@pytest.mark.parametrize("extension", [SYNC_TASK_EXTENSION, ASYNC_TASK_EXTENSION])
def test_a_task_branch_takes_the_tools_parameters_plus_every_task_option(
    branch: Callable[..., Callable[..., Any]], extension: str
) -> None:
    minted = branch(extension)
    assert minted.__name__ == f"sample_tool_{extension}"
    params = inspect.signature(minted).parameters
    assert set(params) == {"x", "note", "count", *BACKEND_TASK_OPTS, "priority"}
    assert params["countdown"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_a_submit_hands_the_door_the_prepared_kwargs_the_options_and_the_callback(
    branch: Callable[..., Callable[..., Any]], door: FakeTaskDoor
) -> None:
    callback = {"tool": "follow_up"}
    await branch(ASYNC_TASK_EXTENSION)(3, countdown=9, eta=None, priority=2, callback_kwargs=callback)

    [submitted] = door.submitted
    # The composed branch hands every tool argument on by name.
    assert submitted.args == ()
    # The omitted ``count`` never ships; the tool name and the caller's capability are stamped.
    assert submitted.kwargs == {
        "x": 3,
        "note": "hi",
        _TOOL_NAME_ARG: "sample_tool",
        WORKER_SECRET_CAPABILITY_ARG: False,
    }
    # An option left ``None`` was not given; the door's own option rides with the platform's.
    assert submitted.options == {"countdown": 9, "priority": 2}
    assert isinstance(submitted.callback, CallbackSchema)
    assert submitted.callback.tool == "follow_up"
    assert WORKER_SECRET_CAPABILITY_ARG in submitted.callback.carried_kwargs


async def test_a_submit_without_a_callback_hands_none(
    branch: Callable[..., Callable[..., Any]], door: FakeTaskDoor
) -> None:
    await branch(ASYNC_TASK_EXTENSION)(1)
    assert door.submitted[0].callback is None


def test_the_sync_branch_keeps_the_tools_description(branch: Callable[..., Callable[..., Any]]) -> None:
    assert branch(SYNC_TASK_EXTENSION).__doc__ == "the doc"


async def test_the_sync_branch_waits_for_the_result_as_fleet_work(
    app: BranchApp, branch: Callable[..., Callable[..., Any]], door: FakeTaskDoor
) -> None:
    assert await branch(SYNC_TASK_EXTENSION)(3) == {"ran": True}
    assert door.waits == [(0, BranchSettings().task_timeout, True)]
    assert app.lifecycle.spans == 1
    assert not app.lifecycle.waiting_on_fleet


async def test_the_sync_branch_names_the_task_left_running_at_its_timeout(
    branch: Callable[..., Callable[..., Any]], door: FakeTaskDoor
) -> None:
    door.error = TaskWaitTimeoutError("still running")
    timeout = BranchSettings().task_timeout
    with pytest.raises(TimeoutError) as excinfo:
        await branch(SYNC_TASK_EXTENSION)(3)
    assert str(excinfo.value) == (f"Task task-0 did not complete within {timeout} seconds; it may still be running.")
    assert not isinstance(excinfo.value, TaskWaitTimeoutError)


async def test_the_sync_branch_raises_a_jobs_own_failure_as_it_is(
    branch: Callable[..., Callable[..., Any]], door: FakeTaskDoor
) -> None:
    failure = TimeoutError("the job's own timeout")
    door.error = failure
    with pytest.raises(TimeoutError) as excinfo:
        await branch(SYNC_TASK_EXTENSION)(3)
    assert excinfo.value is failure


async def test_the_sync_branch_refuses_inside_a_job_and_queues_nothing(
    branch: Callable[..., Callable[..., Any]], door: FakeTaskDoor
) -> None:
    with running_job(), pytest.raises(RuntimeError, match="sample_tool_sync_task cannot wait"):
        await branch(SYNC_TASK_EXTENSION)(3)
    assert door.submitted == []


async def test_the_async_branch_replies_with_the_task_id(branch: Callable[..., Callable[..., Any]]) -> None:
    minted = branch(ASYNC_TASK_EXTENSION)
    assert minted.__doc__ == (
        "Async version of 'sample_tool'. Submits the task to a background queue.\n\nOriginal Doc:\nthe doc"
    )
    assert await minted(1) == {"task_id": "task-0", "status": "submitted"}


@pytest.mark.parametrize("extension", [SYNC_TASK_EXTENSION, ASYNC_TASK_EXTENSION])
def test_a_task_branchs_listed_schema_annotates_the_callback_expressions(
    branch: Callable[..., Callable[..., Any]], extension: str
) -> None:
    parameters = Tool.from_function(branch(extension, plain_tool)).parameters
    callback_def = parameters["$defs"]["CallbackSchema"]["properties"]
    assert callback_def["condition"][EXPRESSION_ANNOTATION_KEY]["label"] == "condition"
    assert callback_def["expr"][EXPRESSION_ANNOTATION_KEY]["label"] == "expression"
    assert EXPRESSION_ANNOTATION_KEY not in callback_def["tool"]
    for name in (*BACKEND_TASK_OPTS, "a", "b"):
        assert EXPRESSION_ANNOTATION_KEY not in parameters["properties"][name]
    assert json.loads(json.dumps(parameters)) == parameters


def test_the_schedule_branch_takes_the_schedule_options_and_the_stamped_keys(
    branch: Callable[..., Callable[..., Any]],
) -> None:
    minted = branch(SCHEDULE_EXTENSION)
    assert minted.__name__ == "sample_tool_schedule_task"
    params = inspect.signature(minted).parameters
    assert set(params) == {"x", "note", "count", SCHEDULE_NAME_KEY, SCHEDULE_CADENCE_KEY, *SCHEDULE_STAMPED_DOOR_OPTS}
    assert inspect.signature(minted).return_annotation is inspect.Signature.empty


async def test_the_schedule_branch_writes_one_enabled_record_naming_the_tool(
    branch: Callable[..., Callable[..., Any]], writer: FakeScheduleWriter
) -> None:
    contract = {"resume_expr": {"content": "."}}
    with schedule_create_fire():
        await branch(SCHEDULE_EXTENSION)(
            7,
            backend_schedule_name="nightly",
            backend_schedule=_HOURLY,
            **{
                SCHEDULE_EXECUTION_KEY_ARG: "svc-key",
                SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
                SCHEDULE_CONTRACT_ARG: contract,
            },
        )

    [record] = writer.written
    assert (record.name, record.tool, record.args, record.enabled) == ("nightly", "sample_tool", [], True)
    assert record.kwargs["x"] == 7
    assert record.cadence == IntervalCadence(every=3600.0)
    # The tool rides on the record alone; the create door's stamps ride in the kwargs.
    assert _TOOL_NAME_ARG not in record.kwargs
    assert "count" not in record.kwargs
    assert record.kwargs[SCHEDULE_EXECUTION_KEY_ARG] == "svc-key"
    assert record.kwargs[SCHEDULE_EXECUTION_FINGERPRINT_ARG] == "fp-1"
    assert record.kwargs[SCHEDULE_CONTRACT_ARG] == contract


async def test_the_schedule_branch_requires_a_name_and_a_valid_cadence(
    branch: Callable[..., Callable[..., Any]], writer: FakeScheduleWriter
) -> None:
    minted = branch(SCHEDULE_EXTENSION)
    with schedule_create_fire(), pytest.raises(ValueError, match="backend_schedule_name is required"):
        await minted(1, backend_schedule=_HOURLY)
    with schedule_create_fire(), pytest.raises(ValidationError):
        await minted(1, backend_schedule_name="nightly", backend_schedule={"type": "bogus"})
    assert writer.written == []


async def test_the_schedule_branch_refuses_a_cadence_the_writer_cannot_honour(
    branch: Callable[..., Callable[..., Any]], writer: FakeScheduleWriter
) -> None:
    relative = {"kind": "interval", "every": 60, "relative": True}
    with (
        schedule_create_fire(),
        pytest.raises(ValueError, match="the acme backend does not support relative interval schedules"),
    ):
        await branch(SCHEDULE_EXTENSION)(1, backend_schedule_name="s", backend_schedule=relative)
    assert writer.written == []


async def test_the_schedule_branch_takes_a_relative_cadence_a_writer_honours(
    branch: Callable[..., Callable[..., Any]], writer: FakeScheduleWriter
) -> None:
    writer.relative_intervals = True
    relative = {"kind": "interval", "every": 60, "relative": True}
    with schedule_create_fire():
        await branch(SCHEDULE_EXTENSION)(1, backend_schedule_name="s", backend_schedule=relative)
    assert writer.written[0].cadence == IntervalCadence(every=60.0, relative=True)


async def test_the_schedule_branch_refuses_outside_the_create_door(
    branch: Callable[..., Callable[..., Any]], writer: FakeScheduleWriter
) -> None:
    with pytest.raises(ReservedScheduleKeyError, match="only through the platform's schedule-create door"):
        await branch(SCHEDULE_EXTENSION)(1, backend_schedule_name="s", backend_schedule=_HOURLY)
    assert writer.written == []


async def _tool_with_its_own_queue(queue: str) -> str:
    """A tool whose own parameter is named like a task option."""
    return queue


@pytest.mark.parametrize("extension", [SYNC_TASK_EXTENSION, ASYNC_TASK_EXTENSION])
def test_a_tool_owning_a_task_options_name_cannot_take_a_task_branch(
    branch: Callable[..., Callable[..., Any]], extension: str
) -> None:
    with pytest.raises(ValueError, match="duplicate parameter name: 'queue'"):
        branch(extension, _tool_with_its_own_queue)
