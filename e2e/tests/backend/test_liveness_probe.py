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
budget. A job blocked outside Python is one the pool cannot end at its budget: past the budget
the worker reads not alive until the job ends. A worker's embedded beat reports into ``<path>-beat`` with
its own pid, and the worker reads alive only while its beat does.

A config reload re-imports the backend's package in each of its processes, and every process
runs on what its launch built across it. A worker whose first connect to the bus withdraws a
stored key another worker's deployment sets reloads before it reads ready: it reads alive and
runs jobs, on the plain worker and on each green pool. After a fleet ``reload_config`` every
live worker reports it applied, every process reads alive, every serving worker runs a job, the
embedded beat's process is running and every liveness file is in place; the stop then reaps the
beat and removes every file.
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

from tai42_e2e import Infra, spawning, wait_for_async
from tai42_e2e.binaries import venv_console_script
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_queue_choice_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready
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
# The long job awaiting its loop, which a worker reports through, and the long job blocked
# outside Python, which its pool cannot end at its budget.
_SLOW_TOOL = "e2e_slow_task"
_BLOCKING_TOOL = "e2e_blocking_task"
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


async def _send_long_job(stack: TaiStack, key: str, arguments: Mapping[str, Any], *, tool: str) -> None:
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            f"{tool}_async_task", {"key": key, "seconds": _LONG_JOB_SECONDS, **arguments}, retry_on_reloading=True
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
    stack: TaiStack, workers: Mapping[str, Mapping[str, Any]], uniq: Callable[[str], str], *, tool: str = _SLOW_TOOL
) -> None:
    """Send each worker a long job of ``tool`` on the queue it serves, and wait until every job runs."""
    keys = {worker: uniq("long-job") for worker in workers}
    for worker, arguments in workers.items():
        await _until_alive(stack, worker)
        await _send_long_job(stack, keys[worker], arguments, tool=tool)

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
    await _start_long_jobs(stack, {solo: {"queue": _SOLO}}, uniq, tool=_BLOCKING_TOOL)

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


# A key the serve worker's deployment sets and the store holds a copy of. A worker whose boot
# cannot read the census applies the stored copy, and withdraws it at its first connect.
_WITHDRAWN_KEY = "TAI_LOG_LEVEL"
_WITHDRAWN_VALUE = "INFO"
_BOOT_FALLBACK_LINE = "deployment ownership for 'boot env bridge': the census could not be read"
_SUBSCRIBE_FAILED_LINE = "worker bus: subscription transport error"
_WITHDRAW_LINE = f"stored env keys ignored, a worker's deployment sets them and its value wins: {_WITHDRAWN_KEY}"
_BOOT_READY_LINE = "app boot-ready: first self-resync complete"
# A worker's restart waits on its boot, its first connect and its reload within this.
_RESTART_DEADLINE = 120.0
# A job's result is read within this.
_RESULT_TIMEOUT = 60
# The census rows outlive a bus outage that spans a worker's boot: no row lapses while its
# process is cut off from the bus.
_OUTAGE_HEARTBEAT_TTL = str(int(2 * _RESTART_DEADLINE))


def _withdrawing_workers(variants: Variants) -> dict[str, dict[str, str]]:
    """The workers whose first connect withdraws the stored key, each with the dispatch arguments of a job it runs.

    The plain worker serves the settings queue; each green-pool worker serves the queue named after its pool."""
    return {"backend": {}, **{f"backend-{pool}": {"queue": pool} for pool in variants.backend.green_pool_workers()}}


def _serving_workers(stack: TaiStack) -> dict[str, dict[str, str]]:
    """The liveness stack's workers, each with the dispatch arguments of a job it runs on the queue it serves."""
    shapes = stack.infra.variants.backend.liveness_processes(stack.resources)
    return {
        "backend": {},
        _QUEUE_WORKER: {"queue": _LABEL},
        **{f"backend-{name}": {"queue": shape.args[shape.args.index("--queue") + 1]} for name, shape in shapes.items()},
    }


def _build_first_connect_withdraw(res: StackResources, variants: Variants) -> StackConfig:
    """The queue-choice stack with its green-pool workers, whose store holds a copy of ``_WITHDRAWN_KEY``.

    The serve worker's deployment sets the key. Each worker of :func:`_withdrawing_workers`
    reports its liveness at the short interval."""
    config = replace(build_queue_choice_stack(res, variants), green_pool_workers=True)
    process_env = {
        **config.process_env,
        "serve": {**config.process_env.get("serve", {}), _WITHDRAWN_KEY: _WITHDRAWN_VALUE},
    }
    for worker in _withdrawing_workers(variants):
        process_env[worker] = {**process_env.get(worker, {}), **_LIVENESS_ENV}
    return replace(config, stored_env={**config.stored_env, _WITHDRAWN_KEY: _WITHDRAWN_VALUE}, process_env=process_env)


def _log_since(stack: TaiStack, process: str, offset: int) -> str:
    return stack.process(process).log_path.read_text(encoding="utf-8", errors="replace")[offset:]


async def _until_logged(stack: TaiStack, process: str, offset: int, line: str) -> None:
    async def logged() -> bool:
        assert stack.process(process).is_running(), stack.process(process).log_tail()
        return line in _log_since(stack, process, offset)

    await wait_for_async(logged, deadline=_RESTART_DEADLINE, interval=0.2, message=f"{process} never logged {line!r}")


async def _restart_withdrawing_at_first_connect(stack: TaiStack, relay: TcpRelay, worker: str) -> str:
    """Restart ``worker`` so that its first connect withdraws the stored key; returns its new life's log.

    The worker stops while it is on the bus, so its census row goes with it. Its new life boots
    with the stack's bus severed: its boot judges ownership by its own deployment env, which does
    not set the key, and applies the stored copy. The other processes' rows stay in the census
    through the outage. Once the worker has tried to subscribe the bus is restored, and its first
    connect reads the census, where the serve worker's row sets the key."""
    offset = len(_log_since(stack, worker, 0))
    stack.process(worker).terminate()
    relay.sever()
    try:
        spawning.spawn(stack, stack.specs[worker])
        await _until_logged(stack, worker, offset, _BOOT_FALLBACK_LINE)
        await _until_logged(stack, worker, offset, _SUBSCRIBE_FAILED_LINE)
    finally:
        relay.restore()
    wait_relay_ready(relay)
    await _until_logged(stack, worker, offset, _BOOT_READY_LINE)
    return _log_since(stack, worker, offset)


async def _run_job(stack: TaiStack, worker: str, dispatch: Mapping[str, str]) -> None:
    """Run an ``e2e_worker_info`` job sent with ``dispatch``; asserts ``worker`` or a child it forked ran it."""
    async with stack.mcp() as mcp:
        queued = await mcp.call_tool("e2e_worker_info_async_task", dict(dispatch), retry_on_reloading=True)
        submitted = queued.data if isinstance(queued.data, dict) else queued.structured_content
        assert isinstance(submitted, dict), submitted
        assert submitted.get("status") == "submitted", submitted
        result = await mcp.call_tool(
            "backend_task_result",
            {"task_id": submitted["task_id"], "timeout": _RESULT_TIMEOUT},
            retry_on_reloading=True,
        )
    info = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(info, dict), info
    info = info.get("result", info)
    assert stack.process(worker).pid in (info["pid"], info["ppid"]), (worker, info, stack.process(worker).log_tail())


# Each worker's restart (the stop's drain, the boot, the first connect's reload) + its job.
@pytest.mark.timeout(900)
async def test_a_worker_whose_first_connect_reloads_before_it_reads_ready_reads_alive_and_runs_jobs(
    infra: Infra, fresh_stack: Callable[..., TaiStack]
) -> None:
    redis_host, redis_port = infra.settings.redis_host_port
    relay = TcpRelay(redis_host, redis_port)
    try:
        relay.start()
        wait_relay_ready(relay)
    except BaseException:
        relay.stop()
        raise
    stack = fresh_stack(
        _build_first_connect_withdraw,
        env_overrides={"TAI_BUS_HEARTBEAT_TTL": _OUTAGE_HEARTBEAT_TTL},
        resource_kwargs={"bus_redis_host": relay.listen_host, "bus_redis_port": relay.port},
        relays=[relay],
    )

    for worker, dispatch in _withdrawing_workers(stack.infra.variants).items():
        life = await _restart_withdrawing_at_first_connect(stack, relay, worker)
        assert life.index(_WITHDRAW_LINE) < life.index(_BOOT_READY_LINE), life[-4000:]
        await _until_alive(stack, worker)
        await _run_job(stack, worker, dispatch)
        await _until_alive(stack, worker)


@pytest.mark.timeout(400)
async def test_after_a_fleet_reload_every_process_runs_whole_and_the_stop_reaps_the_embedded_beat(
    fresh_stack: Callable[..., TaiStack],
) -> None:
    stack = fresh_stack(_build)
    for process in _processes(stack):
        await _until_alive(stack, process)

    report = await stack.api().post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    assert report["ok"] is True, report
    assert {row["outcome"] for row in report["results"]} == {"applied"}, report
    lives = {worker.name for worker in stack.census()}
    assert {row["name"] for row in report["results"]} == lives, (report, lives)

    for worker, dispatch in _serving_workers(stack).items():
        await _run_job(stack, worker, dispatch)
    for process in _processes(stack):
        await _until_alive(stack, process, deadline=30.0)
    beat_pid: int | None = None
    if _EMBEDDED_BEAT in stack.infra.variants.backend.liveness_processes(stack.resources):
        worker = f"backend-{_EMBEDDED_BEAT}"
        beat_pid = int(_beat_file(stack, worker).read_text().splitlines()[0])
        os.kill(beat_pid, 0)
    files = _files(stack)
    assert all(file.exists() for file in files), [file for file in files if not file.exists()]

    stack.teardown()

    assert [file for file in files if file.exists()] == []
    if beat_pid is not None:
        with pytest.raises(ProcessLookupError):
            os.kill(beat_pid, 0)
