"""A backend-worker crash mid-job surfaces a bounded, observable, loud
terminal through the tool-runs API, and the stack is serviceable again once a
worker is restarted. Runs on ALL backend legs (this is where the backends
genuinely diverge).

The run is driven through a ``sync_task`` branch, so the tool-runs supervisor (in
the serve process, which is NOT killed) dispatches the tool to the backend worker
and blocks on the result. The invariant asserted on every backend is that the run
leaves ``running`` for an observable terminal within a bound — never an eternal
``running`` — and the stack is serviceable again after a worker restart.

The concrete terminal diverges per backend, as a real property of each one's
process model — so it is not asserted as a disjunction but read from the backend
variant's declared ``crashed_run_terminal`` and asserted EXACTLY. A backend whose
crash semantics change therefore fails this spec instead of passing under a
weakened assertion:

* arq / celery: the job executes inside the killed process group (arq in the worker
  process itself, celery in its prefork child), so the crash orphans it: the
  ``sync_task`` result wait times out at ``task_timeout`` and the run is recorded
  ``failed`` — a loud, surfaced failure. The sleep is shorter than ``task_timeout``,
  so an unkilled run would have SUCCEEDED; the ``failed`` outcome is caused by the
  crash.
* rq: each job runs in a work-horse child that puts itself in its OWN process group
  (``os.setpgrp``), outside the worker master's group, so killing the master leaves
  the horse running; it completes the job and writes the result, and the run is
  recorded ``succeeded`` — a worker-master crash does not lose the in-flight job on
  this backend."""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import Infra, TaiStack


def _pid_alive(pid: int) -> bool:
    """Whether a process still exists (signal 0 probes without delivering)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# The job sleeps this long; the sync_task result wait is bounded to
# ``_TASK_TIMEOUT`` (> the sleep, so an unkilled run succeeds — the failure below
# is attributable to the crash).
_SLEEP_SECONDS = 6
_TASK_TIMEOUT = 12


async def _run_status(stack: TaiStack, run_id: str) -> str:
    view = await stack.api().get(f"/api/tool-runs/{run_id}")
    return view["status"]


async def _submit_slow_run(stack: TaiStack, key: str, seconds: float) -> str:
    submitted = await stack.api().post(
        "/api/tool-runs",
        json={"tool_name": "e2e_slow_task_sync_task", "arguments": {"key": key, "seconds": seconds}},
        expect=202,
    )
    return submitted["run_id"]


async def test_worker_crash_mid_job_reaches_bounded_terminal_then_recovers(
    infra: Infra, fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    # Bound the backend's sync_task result wait so an orphaned job surfaces a
    # terminal in seconds rather than the multi-minute production default.
    env = infra.variants.backend.task_timeout_env(_TASK_TIMEOUT)
    stack = fresh_stack(env_overrides=env)

    key = uniq("crash")
    run_id = await _submit_slow_run(stack, key, _SLEEP_SECONDS)

    # In-flight IN the worker: the job RPUSHes its ``started`` marker (carrying the
    # executing process's pid) before it sleeps, so a marker on the probe channel
    # proves the job is running in a backend process, not inline in the serve one.
    async def worker_started() -> bool:
        return len(stack.records(key)) >= 1

    await wait_for_async(worker_started, deadline=15.0, message="slow job never started in the backend worker")
    executor_pid = int(json.loads(stack.records(key)[0])["pid"])
    assert await _run_status(stack, run_id) == "running"

    # Crash the backend worker mid-job (SIGKILL the whole process group) and prove the
    # kill really landed on it — the master must have DIED OF OUR SIGNAL, not exited on
    # its own before we got there.
    backend_handle = stack.process("backend")
    stack.kill("backend")
    assert backend_handle.poll() == -signal.SIGKILL, (
        f"the backend worker did not die of our SIGKILL (exit {backend_handle.poll()}); the crash never happened"
    )

    # And the crash reached (or did not reach) the process that was EXECUTING the job,
    # exactly as this backend's process model says it must — the mechanism behind the
    # terminal it declares below.
    expected = infra.variants.backend.crashed_run_terminal
    if expected == "succeeded":
        # rq's work-horse is OUTSIDE the killed group and is still running the job.
        assert _pid_alive(executor_pid), (
            "rq's work-horse should have survived the master's process-group SIGKILL, but it is gone"
        )
    else:
        # The executor was IN the killed group. A SIGKILLed process lingers as a
        # zombie until its (now-dead) parent's reaper collects it, so wait for it to
        # actually disappear rather than racing that async reap.
        async def executor_gone() -> bool:
            return not _pid_alive(executor_pid)

        await wait_for_async(
            executor_gone, deadline=15.0, message=f"the executing process {executor_pid} outlived the worker's crash"
        )

    # The run must reach the terminal this backend declares, within a bound — never
    # an eternal ``running``. The bound is the sync_task wait timeout plus margin.

    async def terminal() -> bool:
        return await _run_status(stack, run_id) != "running"

    await wait_for_async(
        terminal, deadline=_TASK_TIMEOUT + 15.0, message="crashed-worker run never left 'running' (silent hang)"
    )
    view = await stack.api().get(f"/api/tool-runs/{run_id}")
    assert view["status"] == expected, f"expected the {infra.variants.backend.name} crash terminal {expected!r}: {view}"
    if expected == "failed":
        # A surfaced failure is loud — it carries the error, never swallowed.
        assert view.get("error"), view

    # Restart a worker (a new ``tai backend worker`` on the same env) — the system
    # is serviceable again: a NEW background run completes.
    stack.restart("backend")
    recovery_key = uniq("recover")
    recovery_id = await _submit_slow_run(stack, recovery_key, 0.2)

    async def recovered() -> bool:
        return await _run_status(stack, recovery_id) == "succeeded"

    await wait_for_async(recovered, deadline=_TASK_TIMEOUT + 15.0, message="stack not serviceable after worker restart")
