"""A worker run on a green pool through ``tai backend worker`` runs its jobs, reloads, and stops clean.

The stack runs the queue-choice stack plus, for a backend that has them, one worker per green pool,
each serving only the queue named after its pool, and binds a stdio managed MCP server
(``tai42_e2e_fixtures.managed_mcp_server``) on every worker. Such a worker restarts itself once, in
place, through its runtime's prelude: its jobs run in the process the harness started, a job sent
with a countdown runs there once it falls due, a job's callback whose jq ``expr`` maps the job's
result into the follow-up tool's arguments runs there with the mapped values, it runs two slow
jobs as many at once as its pool declares (side by side, or one after the other), on a pool that
can end a job at the job budget a job past it fails with that pool's budget error and the worker
runs the next job (a pool that cannot end one skips that step), it applies a fleet
``reload_config`` (re-probing its stdio MCP child) and runs jobs after it, it logs no fork-gate
refusal, and the harness's stop ends it with exit status 0.
"""

from __future__ import annotations

import itertools
import json
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import MANAGED_SERVER_RELOAD_PROBE_ENV, build_queue_choice_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, StackResources
from tai42_e2e.variants import Variants
from tai42_e2e.variants.backends import GreenPoolWorker

pytestmark = pytest.mark.needs(
    "kind:backend", "probe-tools", "process", "topology", "store:redis", "setting:same-host-pids"
)

_POOLS = ("gevent", "eventlet")
_RESTART_LINE = "restarting with process prelude "
_FORK_GATE_LINE = "fork gate: a fork"
_MCP_TITLE = "greenmcp"
_MCP_TOOL = f"{_MCP_TITLE}_ping"
# Each slow job sleeps this long; two that run one after the other start this far apart.
_SLOW_JOB_SECONDS = 8
# Both slow jobs start within this of being sent when they run side by side, and the second within
# this of the first one's end when they run one after the other.
_BOTH_STARTED_DEADLINE = 6.0
# How much earlier than ``_SLOW_JOB_SECONDS`` after the first the harness may see the second of two
# jobs run one after the other start: the harness sees each start up to one poll late.
_START_SEEN_LATE = 1.0
# The stack's job budget: a slow job runs within it, the overrunning job runs past it.
_BUDGET_SECONDS = 15
_OVERRUN_SECONDS = 30
# A finished job's result is read within this.
_RESULT_TIMEOUT = 60
# The worker exits within this of its SIGTERM.
_EXIT_DEADLINE = 60.0


def _build(res: StackResources, variants: Variants) -> StackConfig:
    """The queue-choice stack with its green-pool workers and a seeded stdio ``mcp`` server.

    The managed server is launched as ``<sut-python> -m tai42_e2e_fixtures.managed_mcp_server``, so
    every worker, the green-pool workers included, starts it as a stdio child at boot and re-probes
    it on every reload within the reload probe budget sized to the child's start. The job budget is
    ``_BUDGET_SECONDS``."""
    base = build_queue_choice_stack(res, variants)
    manifest = {
        **base.manifest,
        "mcp": [
            {
                "title": _MCP_TITLE,
                "config": {"command": sys.executable, "args": ["-m", "tai42_e2e_fixtures.managed_mcp_server"]},
            }
        ],
    }
    env = {**base.env, **MANAGED_SERVER_RELOAD_PROBE_ENV, **variants.backend.task_timeout_env(_BUDGET_SECONDS)}
    return replace(base, manifest=manifest, env=env, green_pool_workers=True)


@pytest.fixture(scope="module")
def green_pool_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("green_pool"), _build)


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


async def _call(stack: TaiStack, tool: str, arguments: dict[str, Any]) -> Any:
    async with stack.mcp() as mcp:
        return await mcp.call_tool(tool, arguments)


async def _send(stack: TaiStack, tool: str, arguments: dict[str, Any]) -> str:
    """Queue ``tool``'s ``async_task`` branch and return the job id."""
    queued = _tool_map(await _call(stack, f"{tool}_async_task", arguments))
    assert queued["status"] == "submitted", queued
    return str(queued["task_id"])


async def _result(stack: TaiStack, task_id: str) -> Any:
    return await _call(stack, "backend_task_result", {"task_id": task_id, "timeout": _RESULT_TIMEOUT})


async def _worker_info(stack: TaiStack, pool: str, **dispatch: Any) -> dict[str, Any]:
    """The identity of the process that ran an ``e2e_worker_info`` job sent to ``pool``'s queue with ``dispatch``."""
    return _tool_map(await _result(stack, await _send(stack, "e2e_worker_info", {"queue": pool, **dispatch})))


async def _reload_fleet(stack: TaiStack) -> dict[str, str]:
    """Fan a fleet ``reload_config`` out and return each worker's outcome by its bus slot name."""
    async with stack.mcp() as mcp:
        # Prime the session's tool-output-schema cache: the reload retires this session, so a
        # follow-up tools/list after the call would find it terminated.
        await mcp.list_tools()
        result = await mcp.call_tool("reload_config", {})
    report = _tool_map(result)
    assert report["op"] == "reload_config", report
    assert report["reachable"] is True, report
    return {row["name"]: row["outcome"] for row in report["results"]}


async def _jq_callback_record(stack: TaiStack, pool: str, key: str) -> dict[str, Any]:
    """Send an ``e2e_worker_info`` job to ``pool``'s queue with a callback whose jq ``expr`` maps the
    job's result into an ``e2e_record`` call under ``key``; return the record the callback left."""
    callback = {"tool": "e2e_record", "expr": {"content": f'{{key: "{key}", value: (.pid | tostring)}}'}}
    await _result(stack, await _send(stack, "e2e_worker_info", {"queue": pool, "callback_kwargs": callback}))

    async def recorded() -> dict[str, Any] | None:
        records = stack.records(key)
        return json.loads(records[0]) if records else None

    return await wait_for_async(
        recorded,
        deadline=_RESULT_TIMEOUT,
        interval=0.2,
        message=f"the {pool} worker's jq callback left no record",
    )


async def _start_slow_jobs(stack: TaiStack, pool: str, green: GreenPoolWorker, keys: list[str]) -> list[str]:
    """Send a slow job per key to ``pool``'s queue and wait until each has started, as many at once as
    the pool runs: side by side within ``_BOTH_STARTED_DEADLINE``, or one after the other, the next
    starting no earlier than ``_SLOW_JOB_SECONDS`` after the one before. Returns the job ids and asserts
    the worker ran them."""
    task_ids = [
        await _send(stack, "e2e_slow_task", {"key": key, "seconds": _SLOW_JOB_SECONDS, "queue": pool}) for key in keys
    ]
    seen_at: dict[str, float] = {}

    async def all_started() -> list[dict[str, Any]] | None:
        now = time.monotonic()
        for key in keys:
            if key not in seen_at and stack.records(key):
                seen_at[key] = now
        return [json.loads(stack.records(key)[0]) for key in keys] if len(seen_at) == len(keys) else None

    side_by_side = green.jobs_at_once >= len(keys)
    started = await wait_for_async(
        all_started,
        deadline=_BOTH_STARTED_DEADLINE
        if side_by_side
        else _SLOW_JOB_SECONDS * (len(keys) - 1) + _BOTH_STARTED_DEADLINE,
        interval=0.2,
        message=f"the {pool} worker did not start its slow jobs {'side by side' if side_by_side else 'in turn'}",
    )
    assert [record["pid"] for record in started] == [stack.process(f"backend-{pool}").pid] * len(keys), started
    if not side_by_side:
        starts = sorted(seen_at.values())
        gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
        assert all(gap >= _SLOW_JOB_SECONDS - _START_SEEN_LATE for gap in gaps), (gaps, seen_at)
    return task_ids


async def _overrun_the_budget(stack: TaiStack, pool: str, past_budget_error: str, uniq: Callable[[str], str]) -> None:
    """A job that runs past the job budget fails with ``past_budget_error`` in its failure."""
    key = uniq(f"{pool}-overrun")
    task_id = await _send(stack, "e2e_slow_task", {"key": key, "seconds": _OVERRUN_SECONDS, "queue": pool})
    with pytest.raises(ToolError, match=past_budget_error):
        await _result(stack, task_id)
    assert stack.records(key), "the overrunning job never started"


@pytest.mark.parametrize("pool", _POOLS)
# The stack boot inside the first case's timer (120) + the first job (60) + the delayed job (60) + the
# job with a jq callback and its record (2 x 60) + the slow jobs (2 x 8 + 2 x 60) + the overrunning
# job (60) + the job after it (60) + the reload call's read bound (210) + the job after it (60) + the
# stop (60).
@pytest.mark.timeout(1000)
async def test_a_green_pool_worker_runs_its_jobs_reloads_and_stops_clean(
    green_pool_stack: TaiStack, pool: str, uniq: Callable[[str], str]
) -> None:
    stack = green_pool_stack
    green = stack.infra.variants.backend.green_pool_workers().get(pool)
    if green is None:
        pytest.skip(f"the {stack.infra.variants.backend.name} backend runs no {pool} worker")
    worker = stack.process(f"backend-{pool}")

    info = await _worker_info(stack, pool)
    assert info["pid"] == worker.pid, (info, worker.log_tail())
    async with stack.mcp() as mcp:
        tools = [tool.name for tool in await mcp.list_tools()]
    assert _MCP_TOOL in tools, tools

    delayed = await _worker_info(stack, pool, countdown=1)
    assert delayed["pid"] == worker.pid, (delayed, worker.log_tail())

    record = await _jq_callback_record(stack, pool, uniq(f"{pool}-jq-callback"))
    assert record == {"value": str(worker.pid), "pid": worker.pid}, (record, worker.log_tail())

    task_ids = await _start_slow_jobs(stack, pool, green, [uniq(f"{pool}-slow") for _ in range(2)])
    for task_id in task_ids:
        await _result(stack, task_id)

    if green.past_budget_error is not None:
        await _overrun_the_budget(stack, pool, green.past_budget_error, uniq)
        after_overrun = await _worker_info(stack, pool)
        assert after_overrun["pid"] == worker.pid, (after_overrun, worker.log_tail())

    outcomes = await _reload_fleet(stack)
    assert outcomes.get(info["name"]) == "applied", (info["name"], outcomes, worker.log_tail())

    after_reload = await _worker_info(stack, pool)
    assert after_reload["pid"] == worker.pid, (after_reload, worker.log_tail())

    # Stopped as the harness stops a process of the stack, under the lock every process change
    # of the stack is serialised on.
    with stack.supervisor.lock:
        worker.terminate(sigterm_grace=_EXIT_DEADLINE)
    assert worker.poll() == 0, worker.log_tail()
    log = worker.log_path.read_text(encoding="utf-8", errors="replace")
    assert log.count(_RESTART_LINE) == 1, worker.log_tail()
    assert _FORK_GATE_LINE not in log, log[log.index(_FORK_GATE_LINE) :][:2000]
