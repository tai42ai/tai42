"""Every backend loop process reports its progress into its own liveness file, read by the probe.

The stack runs the backend worker, a second worker serving the queue labelled ``b``, the
variant's beat where it has one (rq and celery), and the variant's liveness process shapes
(rq: a ``--pool solo`` worker; celery: a ``-P solo`` worker and a worker embedding its beat
with ``-B``). Each process
writes the file named by its own ``TAI_BACKEND_LIVENESS_PATH``; ``tai42-backend-probe`` run
in that process's env reads it: alive (exit 0) while the process passes its loop, including
through a job longer than the file's horizon plus the grace, and not alive (exit 1) once the
process is stopped. A process that exits removes its file.

A ``solo`` worker runs its job in its own process, so the worker's progress is reported
while the job runs (rq: by a monitor thread; celery: by the job's own loop), up to the job
budget: past it the pool cannot end a job blocked outside Python, and the worker reads not
alive until the job ends. A worker's embedded beat reports into ``<path>-beat`` with
its own pid, and the worker reads alive only while its beat does.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.binaries import venv_console_script
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_queue_choice_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, StackResources
from tai42_e2e.variants import Variants
from tai42_e2e.waiting import wait_absent_async

pytestmark = pytest.mark.needs(
    "kind:backend", "probe-tools", "process", "topology", "store:redis", "setting:same-host-pids"
)

_LABEL = "b"
_QUEUE_WORKER = f"backend-queue-{_LABEL}"

# A refresh interval and a grace short enough for a stopped process to read not alive
# within seconds.
_INTERVAL = 1.0
_GRACE = 2.0
_LIVENESS_ENV = {"TAI_BACKEND_LIVENESS_INTERVAL": f"{_INTERVAL:g}", "TAI_BACKEND_LIVENESS_GRACE": f"{_GRACE:g}"}

# A job longer than any process's horizon plus the grace, so a file refreshed only between
# jobs would read not alive while it runs.
_LONG_JOB_SECONDS = 12
# One probe run starts an interpreter; a verdict lands this much after the file turned stale.
_PROBE_RUN_SECONDS = 1.0

# The liveness process shapes these tests drive, by their variant name.
_SOLO = "solo"
_EMBEDDED_BEAT = "embedded-beat"
# The job budget the budget test holds the solo worker to.
_SOLO_BUDGET_SECONDS = 3


def _loop_processes(variants: Variants, res: StackResources) -> list[str]:
    """The stack's backend processes that write a liveness file."""
    extras = [f"backend-{args[0]}" for args in variants.backend.extra_backend_processes()]
    shapes = [f"backend-{name}" for name in variants.backend.liveness_processes(res)]
    return ["backend", _QUEUE_WORKER, *extras, *shapes]


def _build(res: StackResources, variants: Variants) -> StackConfig:
    config = replace(build_queue_choice_stack(res, variants), liveness_processes=True)
    process_env = dict(config.process_env)
    for name in _loop_processes(variants, res):
        process_env[name] = {**process_env.get(name, {}), **_LIVENESS_ENV}
    return replace(config, process_env=process_env)


def _build_solo_budget(res: StackResources, variants: Variants) -> StackConfig:
    """The liveness stack with the solo worker, where the variant has one, held to a short job budget."""
    config = _build(res, variants)
    solo = f"backend-{_SOLO}"
    if solo not in config.process_env:
        return config
    budget_env = variants.backend.task_timeout_env(_SOLO_BUDGET_SECONDS)
    return replace(config, process_env={**config.process_env, solo: {**config.process_env[solo], **budget_env}})


@pytest.fixture(scope="module")
def liveness_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("liveness"), _build)


@dataclass(frozen=True)
class _Verdict:
    code: int
    line: str

    @property
    def alive(self) -> bool:
        return self.code == 0


async def _probe(stack: TaiStack, process: str) -> _Verdict:
    """Run the probe in ``process``'s own env, as an orchestrator's exec probe does."""
    child = await asyncio.create_subprocess_exec(
        venv_console_script("tai42-backend-probe"),
        env=stack.process(process).env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await child.communicate()
    assert child.returncode is not None
    return _Verdict(child.returncode, out.decode().strip())


async def _until_alive(stack: TaiStack, process: str, deadline: float = 60.0) -> _Verdict:
    async def alive() -> _Verdict | None:
        assert stack.process(process).is_running(), stack.process(process).log_tail()
        verdict = await _probe(stack, process)
        return verdict if verdict.alive else None

    return await wait_for_async(alive, deadline=deadline, interval=0.5, message=f"{process} never read alive")


def _horizon(stack: TaiStack, process: str) -> float:
    """The horizon the process last promised: the second line of its file."""
    return float((stack.liveness_dir / process).read_text().splitlines()[1])


def _processes(stack: TaiStack) -> list[str]:
    return _loop_processes(stack.infra.variants, stack.resources)


def _shape(stack: TaiStack, name: str) -> str:
    """The process running the variant's liveness shape ``name``; skips where the variant has none."""
    backend = stack.infra.variants.backend
    if name not in backend.liveness_processes(stack.resources):
        pytest.skip(f"{backend.name}: runs no {name} worker shape")
    return f"backend-{name}"


def _beat_file(stack: TaiStack, process: str) -> Path:
    """The liveness file the embedded beat of worker ``process`` writes."""
    return stack.liveness_dir / f"{process}-beat"


def _files(stack: TaiStack) -> list[Path]:
    """Every liveness file the stack's processes write, the embedded beat's included."""
    files = [stack.liveness_dir / process for process in _processes(stack)]
    if _EMBEDDED_BEAT in stack.infra.variants.backend.liveness_processes(stack.resources):
        files.append(_beat_file(stack, f"backend-{_EMBEDDED_BEAT}"))
    return files


@pytest.mark.timeout(300)
async def test_each_loop_process_reads_alive_with_its_own_pid(liveness_stack: TaiStack) -> None:
    stack = liveness_stack
    for process in _processes(stack):
        verdict = await _until_alive(stack, process)
        assert f"pid {stack.process(process).pid} " in verdict.line, (process, verdict.line)
        assert str(stack.liveness_dir / process) in verdict.line, (process, verdict.line)


async def _send_long_job(stack: TaiStack, key: str, arguments: Mapping[str, Any]) -> None:
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "e2e_slow_task_async_task", {"key": key, "seconds": _LONG_JOB_SECONDS, **arguments}, retry_on_reloading=True
        )
    queued = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(queued, dict), queued
    assert queued.get("status") == "submitted", queued


@pytest.mark.timeout(300)
async def test_a_worker_running_a_job_longer_than_its_horizon_reads_alive_throughout(
    liveness_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    await _assert_alive_through_long_jobs(liveness_stack, {"backend": {}, _QUEUE_WORKER: {"queue": _LABEL}}, uniq)


@pytest.mark.timeout(300)
async def test_a_solo_worker_running_a_job_longer_than_its_horizon_reads_alive_throughout(
    liveness_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = liveness_stack
    await _assert_alive_through_long_jobs(stack, {_shape(stack, _SOLO): {"queue": _SOLO}}, uniq)


async def _start_long_jobs(
    stack: TaiStack, workers: Mapping[str, Mapping[str, Any]], uniq: Callable[[str], str]
) -> None:
    """Send each worker a long job on the queue it serves, and wait until every job runs."""
    keys = {worker: uniq("long-job") for worker in workers}
    for worker, arguments in workers.items():
        await _until_alive(stack, worker)
        await _send_long_job(stack, keys[worker], arguments)

    async def started() -> bool:
        return all(stack.records(key) for key in keys.values())

    await wait_for_async(started, deadline=30.0, message="a long job never started on its worker")


async def _assert_alive_through_long_jobs(
    stack: TaiStack, workers: Mapping[str, Mapping[str, Any]], uniq: Callable[[str], str]
) -> None:
    await _start_long_jobs(stack, workers, uniq)
    window = _LONG_JOB_SECONDS - 1.0
    assert window > max(_horizon(stack, worker) for worker in workers) + _GRACE

    async def a_worker_reads_not_alive() -> str | None:
        for worker in workers:
            verdict = await _probe(stack, worker)
            if not verdict.alive:
                return f"{worker}: {verdict.line}"
        return None

    await wait_absent_async(
        a_worker_reads_not_alive, window=window, message="a worker read not alive while it ran a long job"
    )


@pytest.mark.timeout(300)
async def test_a_solo_worker_whose_job_runs_past_its_budget_reads_not_alive_until_the_job_ends(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(_build_solo_budget)
    solo = _shape(stack, _SOLO)
    started = time.monotonic()
    await _start_long_jobs(stack, {solo: {"queue": _SOLO}}, uniq)

    # The job's last report is no later than the moment it passed its budget, so the file is
    # stale by its horizon plus the grace after it.
    bound = _SOLO_BUDGET_SECONDS + _horizon(stack, solo) + _GRACE + _PROBE_RUN_SECONDS

    async def not_alive() -> _Verdict | None:
        verdict = await _probe(stack, solo)
        return None if verdict.alive else verdict

    verdict = await wait_for_async(
        not_alive, deadline=bound, interval=0.2, message=f"{solo} read alive with its job past its budget"
    )
    assert verdict.line.startswith("not alive: last report "), verdict.line
    assert time.monotonic() - started < _LONG_JOB_SECONDS, "the job ended before the worker read not alive"
    await _until_alive(stack, solo, deadline=_LONG_JOB_SECONDS + 30.0)


@pytest.mark.timeout(300)
async def test_a_worker_reads_alive_only_while_its_embedded_beat_does(liveness_stack: TaiStack) -> None:
    stack = liveness_stack
    worker = _shape(stack, _EMBEDDED_BEAT)
    await _until_alive(stack, worker)
    beat_file = _beat_file(stack, worker)
    beat_pid = int(beat_file.read_text().splitlines()[0])
    assert beat_pid != stack.process(worker).pid
    os.kill(beat_pid, 0)

    beat_horizon = float(beat_file.read_text().splitlines()[1])
    os.kill(beat_pid, signal.SIGSTOP)
    try:
        # The beat's file is stale by its horizon plus the grace after the stop; the worker's
        # next tick withholds its report, so its own file is stale by its horizon plus the
        # grace after that.
        bound = beat_horizon + _GRACE + _horizon(stack, worker) + _GRACE + _PROBE_RUN_SECONDS

        async def not_alive() -> _Verdict | None:
            verdict = await _probe(stack, worker)
            return None if verdict.alive else verdict

        verdict = await wait_for_async(
            not_alive, deadline=bound, interval=0.2, message=f"{worker} read alive while its beat was stopped"
        )
        assert verdict.line.startswith("not alive: last report "), verdict.line
        assert stack.process(worker).is_running(), stack.process(worker).log_tail()
    finally:
        os.kill(beat_pid, signal.SIGCONT)
    await _until_alive(stack, worker, deadline=30.0)


@pytest.mark.timeout(300)
async def test_a_stopped_process_reads_not_alive_and_alive_again_once_continued(liveness_stack: TaiStack) -> None:
    stack = liveness_stack
    for process in _processes(stack):
        await _until_alive(stack, process)
        pid = stack.process(process).pid
        os.kill(pid, signal.SIGSTOP)
        try:
            # The last report is no later than the stop, so the file is stale by its horizon plus
            # the grace after it.
            bound = _horizon(stack, process) + _GRACE + _PROBE_RUN_SECONDS

            async def not_alive(process: str = process) -> _Verdict | None:
                verdict = await _probe(stack, process)
                return None if verdict.alive else verdict

            verdict = await wait_for_async(
                not_alive, deadline=bound, interval=0.2, message=f"{process} read alive while stopped"
            )
            assert verdict.code == 1, verdict
            assert verdict.line.startswith("not alive: last report "), verdict.line
        finally:
            os.kill(pid, signal.SIGCONT)
        await _until_alive(stack, process, deadline=30.0)


@pytest.mark.timeout(300)
async def test_a_process_removes_its_file_when_the_stack_stops(
    fresh_stack: Callable[..., TaiStack],
) -> None:
    stack = fresh_stack(_build)
    for process in _processes(stack):
        await _until_alive(stack, process)
    files = _files(stack)
    assert all(file.exists() for file in files), [file for file in files if not file.exists()]

    stack.teardown()

    assert [file for file in files if file.exists()] == []
