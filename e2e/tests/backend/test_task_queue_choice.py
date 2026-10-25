"""A task sent with ``queue`` runs on the worker serving that queue.

The stack runs the backend worker on the settings queue and a second worker serving only
the queue labelled ``b``. A job sent with ``queue="b"`` runs on the second worker, through
the MCP branch call, a preset that fixes the label, and the tool-runs submit door; a job
sent without it runs on the first. The by-id tools read a ``b`` job's status, result and
cancel, and the scheduled listing names a ``b`` job held for later.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_queue_choice_stack
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:backend", "probe-tools", "process", "topology")

_LABEL = "b"
_QUEUE_WORKER = f"backend-queue-{_LABEL}"

# Each engine's own status word for a finished job, for a job held for later, and its
# answer to a cancel of that held job.
_DONE = {"arq": "complete", "rq": "finished", "celery": "SUCCESS"}
_HELD = {"arq": "deferred", "rq": "scheduled", "celery": "PENDING"}
_CANCELLED = {"arq": "Task {} aborted", "rq": "Task {} canceled", "celery": "Task {} revoked (terminate=False)"}
# The status after the cancel: arq stores the abort as the job's outcome, RQ deletes the
# job, and the Celery worker holding the job records it revoked once the revoke reaches it.
_AFTER_CANCEL = {"arq": "complete", "rq": "unknown", "celery": "REVOKED"}

# The worker serves a created preset within this.
_PRESET_SERVED_DEADLINE = 15.0
# A finished job's result is read within this.
_RESULT_TIMEOUT = 60


@pytest.fixture(scope="module")
def queue_choice_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("queue_choice"), build_queue_choice_stack)


def _engine(stack: TaiStack) -> str:
    return stack.infra.variants.backend.name


def _tool_map(result: Any) -> dict[str, Any]:
    """The map a tool call returned, from the deserialized ``data`` or the ``{"result": …}`` envelope."""
    if isinstance(result.data, dict):
        return result.data
    content = result.structured_content
    if isinstance(content, dict) and isinstance(content.get("result"), dict):
        return content["result"]
    if isinstance(content, dict):
        return content
    raise AssertionError(f"the tool call returned no map: {result!r}")


def _tool_text(result: Any) -> str:
    """A tool call's scalar payload as text."""
    if result.data is not None:
        return str(result.data)
    if isinstance(result.structured_content, dict) and "result" in result.structured_content:
        return str(result.structured_content["result"])
    return "".join(getattr(block, "text", "") for block in (result.content or []))


async def _call(stack: TaiStack, tool: str, arguments: dict[str, Any]) -> Any:
    async with stack.mcp() as mcp:
        return await mcp.call_tool(tool, arguments, retry_on_reloading=True)


async def _send(stack: TaiStack, tool: str, arguments: dict[str, Any]) -> str:
    """Queue ``tool``'s ``async_task`` branch and return the job id."""
    queued = _tool_map(await _call(stack, f"{tool}_async_task", arguments))
    assert queued["status"] == "submitted", queued
    return str(queued["task_id"])


def _ran_on(stack: TaiStack, info: dict[str, Any], process: str) -> bool:
    """Whether the worker-info probe ran in ``process`` or in a child it forked for the job."""
    pid = stack.process(process).pid
    return pid in (info["pid"], info["ppid"])


def _assert_ran_on(stack: TaiStack, info: dict[str, Any], process: str) -> None:
    others = {name: stack.process(name).pid for name in ("backend", _QUEUE_WORKER)}
    assert _ran_on(stack, info, process), f"ran in pid {info['pid']} (ppid {info['ppid']}), not on {process}: {others}"


@pytest.mark.timeout(345)
async def test_a_job_sent_to_a_queue_runs_on_the_worker_serving_it(queue_choice_stack: TaiStack) -> None:
    stack = queue_choice_stack
    task_id = await _send(stack, "e2e_worker_info", {"queue": _LABEL})

    info = _tool_map(await _call(stack, "backend_task_result", {"task_id": task_id, "timeout": _RESULT_TIMEOUT}))
    _assert_ran_on(stack, info, _QUEUE_WORKER)
    status = _tool_text(await _call(stack, "backend_task_status", {"task_id": task_id}))
    assert status == _DONE[_engine(stack)]


@pytest.mark.timeout(345)
async def test_a_job_sent_without_a_queue_runs_on_the_settings_queue_worker(queue_choice_stack: TaiStack) -> None:
    stack = queue_choice_stack
    task_id = await _send(stack, "e2e_worker_info", {})

    info = _tool_map(await _call(stack, "backend_task_result", {"task_id": task_id, "timeout": _RESULT_TIMEOUT}))
    _assert_ran_on(stack, info, "backend")


@pytest.mark.timeout(345)
async def test_a_run_submitted_over_http_with_a_queue_runs_on_the_worker_serving_it(
    queue_choice_stack: TaiStack,
) -> None:
    stack = queue_choice_stack
    api = stack.api()
    submitted = await api.post(
        "/api/tool-runs",
        json={"tool_name": "e2e_worker_info_sync_task", "arguments": {"queue": _LABEL}},
        expect=202,
        retry_on_reloading=True,
    )
    run_id = submitted["run_id"]

    async def succeeded() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        assert view["status"] != "failed", view
        return view if view["status"] == "succeeded" else None

    view = await wait_for_async(succeeded, deadline=_RESULT_TIMEOUT, message=f"run {run_id} never succeeded")
    _assert_ran_on(stack, view["result"], _QUEUE_WORKER)


@pytest.mark.timeout(345)
@pytest.mark.needs("mutable")
async def test_a_preset_fixing_the_queue_runs_on_the_worker_serving_it(
    queue_choice_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = queue_choice_stack
    name = uniq("queue_preset")
    await stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_worker_info_sync_task",
            "description": "worker info on the b queue",
            "fixed_kwargs": {"queue": _LABEL},
        },
        retry_on_reloading=True,
    )

    async def served() -> dict[str, Any] | None:
        try:
            return _tool_map(await _call(stack, name, {}))
        except ToolError as exc:
            text = str(exc)
            if "reloading" in text or f"Unknown tool: '{name}'" in text:
                return None
            raise

    info = await wait_for_async(served, deadline=_PRESET_SERVED_DEADLINE, message=f"{name} was never served")
    _assert_ran_on(stack, info, _QUEUE_WORKER)


@pytest.mark.timeout(345)
async def test_a_job_held_for_later_on_a_queue_is_read_listed_and_cancelled_by_id(
    queue_choice_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = queue_choice_stack
    engine = _engine(stack)
    task_id = await _send(stack, "e2e_slow_task", {"key": uniq("held"), "seconds": 1, "queue": _LABEL, "countdown": 30})

    assert _tool_text(await _call(stack, "backend_task_status", {"task_id": task_id})) == _HELD[engine]

    async def listed() -> bool:
        scheduled = _tool_map(await _call(stack, "backend_scheduled_tasks", {}))
        return task_id in json.dumps(scheduled)

    await wait_for_async(listed, deadline=20.0, message=f"{task_id} never listed as scheduled")

    cancelled = _tool_text(await _call(stack, "backend_cancel_task", {"task_id": task_id}))
    assert cancelled == _CANCELLED[engine].format(task_id)

    after = _AFTER_CANCEL[engine]

    async def settled() -> bool:
        return _tool_text(await _call(stack, "backend_task_status", {"task_id": task_id})) == after

    await wait_for_async(settled, deadline=20.0, message=f"{task_id} never read {after!r} after the cancel")


@pytest.mark.timeout(345)
async def test_a_queue_that_is_not_a_label_is_refused(queue_choice_stack: TaiStack) -> None:
    stack = queue_choice_stack
    if _engine(stack) == "celery":
        pytest.skip("celery: a queue name is free-form, so no value is refused")
    with pytest.raises(ToolError, match="queue label 'b:x' must match"):
        await _call(stack, "e2e_worker_info_async_task", {"queue": "b:x"})
