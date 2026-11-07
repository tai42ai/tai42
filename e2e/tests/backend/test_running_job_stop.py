"""``backend_cancel_task(terminate=True)`` on a running job, on each worker pool the backend runs.

The stack runs the queue-choice stack plus the variant's liveness process shapes and green-pool
workers, so beside the backend worker on the settings queue it runs, where the variant has them,
a ``solo`` worker and one worker per green pool, each serving only the queue named after it. A slow
job sent to each worker is cancelled with ``terminate`` once it has started, and the reply says
what was done: a worker whose pool ends a running job ends it, and the job reads with its engine's
status for an ended job long before its own end; a celery ``solo`` worker reads no control message
while it runs a job, so the stop is not confirmed and the job runs to its end.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_queue_choice_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, StackResources
from tai42_e2e.variants import Variants

pytestmark = pytest.mark.needs("kind:backend", "probe-tools", "process", "topology", "store:redis")

# The worker a lane's job is sent to: ``None`` is the backend worker on the settings queue; any
# other is the worker serving only the queue of that name.
_LANES = (None, "solo", "gevent", "eventlet")
# The slow job sleeps this long; a stopped job reads as ended well within it.
_JOB_SECONDS = 25
_STOPPED_WITHIN = 15.0
# A job that is not stopped runs to its end within this.
_RESULT_TIMEOUT = 60
# A slow job starts within this of being sent.
_STARTED_DEADLINE = 30.0

# Each engine's reply to the stop of a job its worker ended, and the job's status after it.
_STOPPED = {"arq": "Task {} aborted", "rq": "Task {} stopped", "celery": "Task {} revoked and stopped"}
_AFTER_STOP = {"arq": "complete", "rq": "stopped", "celery": "REVOKED"}
# The lanes whose worker reads the stop only once the job has ended, with that reply: the stack's other
# workers hear the revoke and store REVOKED as the task's state before they answer.
_NOT_STOPPED: dict[tuple[str, str | None], str] = {
    ("celery", "solo"): "Task {} stop requested but not confirmed (status: REVOKED)"
}
# The status of a job that ran to its end, on the engines with a lane above.
_DONE = {"celery": "SUCCESS"}


def _build(res: StackResources, variants: Variants) -> StackConfig:
    """The queue-choice stack with the variant's ``solo`` worker and green-pool workers."""
    return replace(build_queue_choice_stack(res, variants), liveness_processes=True, green_pool_workers=True)


@pytest.fixture(scope="module")
def stop_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("running_job_stop"), _build)


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
        return await mcp.call_tool(tool, arguments)


def _runs_lane(stack: TaiStack, lane: str | None) -> bool:
    backend = stack.infra.variants.backend
    return lane is None or lane in backend.liveness_processes(stack.resources) or lane in backend.green_pool_workers()


@pytest.mark.parametrize("lane", _LANES, ids=lambda lane: lane or "settings-queue")
# The stack boot inside the first case's timer (180) + the job's start (30) + the stop and its
# reply (60) + the job's end (60).
@pytest.mark.timeout(345)
async def test_a_stop_of_a_running_job_replies_what_its_worker_did(
    stop_stack: TaiStack, lane: str | None, uniq: Callable[[str], str]
) -> None:
    stack = stop_stack
    engine = stack.infra.variants.backend.name
    if not _runs_lane(stack, lane):
        pytest.skip(f"the {engine} backend runs no {lane} worker")
    key = uniq(f"stop-{lane or 'settings-queue'}")
    arguments: dict[str, Any] = {"key": key, "seconds": _JOB_SECONDS, **({"queue": lane} if lane else {})}
    queued = _tool_map(await _call(stack, "e2e_slow_task_async_task", arguments))
    assert queued["status"] == "submitted", queued
    task_id = str(queued["task_id"])

    async def started() -> bool | None:
        return True if stack.records(key) else None

    await wait_for_async(started, deadline=_STARTED_DEADLINE, interval=0.2, message=f"{task_id} never started")

    stopped_at = time.monotonic()
    reply = _tool_text(await _call(stack, "backend_cancel_task", {"task_id": task_id, "terminate": True}))

    not_stopped = _NOT_STOPPED.get((engine, lane))
    if not_stopped is not None:
        assert reply == not_stopped.format(task_id)
        done = _DONE[engine]

        # The status reads REVOKED, a ready state, until the job's own end stores its result, so
        # the result is read once the status says the job has ended.
        async def finished() -> bool | None:
            return True if _tool_text(await _call(stack, "backend_task_status", {"task_id": task_id})) == done else None

        await wait_for_async(finished, deadline=_RESULT_TIMEOUT, message=f"{task_id} never read {done!r}")
        assert _tool_text(await _call(stack, "backend_task_result", {"task_id": task_id})) == "done"
        return
    assert reply == _STOPPED[engine].format(task_id)
    after = _AFTER_STOP[engine]

    async def ended() -> bool | None:
        return True if _tool_text(await _call(stack, "backend_task_status", {"task_id": task_id})) == after else None

    await wait_for_async(ended, deadline=_STOPPED_WITHIN, message=f"{task_id} never read {after!r} after the stop")
    assert time.monotonic() - stopped_at < _STOPPED_WITHIN
