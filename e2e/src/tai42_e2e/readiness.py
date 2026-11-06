"""Readiness waits for the boot engine: HTTP health and the worker-bus census, read until every
expected serve worker's presence row reads ``ready`` and every backend process has a presence row
under its own pid, so a test acting the instant the stack fixture returns never races an incomplete
fleet or a serve worker still in its boot gate."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from tai42_e2e.child_env import needs_bus
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from tai42_e2e.stack import TaiStack
    from tai42_e2e.variants import BusWorker


def wait_ready(stack: TaiStack) -> None:
    deadline = stack.infra.settings.boot_timeout
    for port in stack.app_ports:
        wait_http_ok(stack, f"{stack.base_url(port)}/health", deadline, "app health")
    if stack.config.run_metrics:
        assert stack.metrics_port is not None
        wait_http_ok(stack, f"http://{stack.host}:{stack.metrics_port}/metrics", deadline, "metrics")
    wait_fleet_converged(stack, deadline)


def ready_status(stack: TaiStack, *, port: int | None = None) -> tuple[int, dict[str, Any]]:
    """Read ``/ready`` ONCE for one app port, returning its status code and parsed body.

    Unlike :func:`wait_ready`, which blocks until the fleet is green, this reads the
    live readiness signal a single time — a caller asserting a transient readiness
    state (a dead perpetual task 503ing before the process graceful-exits, naming the
    task under a ``perpetual_task:<name>`` check) reads it here rather than waiting for
    green."""
    target = port if port is not None else stack.app_ports[0]
    resp = httpx.get(f"{stack.base_url(target)}/ready", timeout=5.0)
    return resp.status_code, resp.json()


def assert_no_dead_perpetual_task(stack: TaiStack, *, port: int | None = None) -> dict[str, Any]:
    """Assert ``/ready`` is 200 and names NO dead perpetual task, returning its checks.

    While the five perpetual tasks (the worker-bus subscription, the failed-MCP
    re-probe loop, the interactions expiry reaper, the sandbox reaper, the inbound-media
    retention reaper) all run, the readiness marker is unset, so ``/ready`` readies with
    no ``perpetual_task:*`` check — the marker read must not false-positive on a healthy
    fleet."""
    status, body = ready_status(stack, port=port)
    assert status == 200, f"/ready not ready with all perpetual tasks alive: {status} {body}"
    checks = body["checks"]
    dead = [name for name in checks if name.startswith("perpetual_task:")]
    assert not dead, f"/ready named a dead perpetual task on a healthy fleet: {dead} in {checks}"
    return checks


def assert_ready_names_dead_perpetual_task(
    stack: TaiStack, task_name: str, reason: str, *, port: int | None = None
) -> dict[str, Any]:
    """Assert ``/ready`` is 503 and names ``task_name`` dead with ``reason``.

    A perpetual task whose death is marked flips ``/ready`` to 503 ``not_ready`` with an
    additive ``perpetual_task:<task_name>`` check carrying the failure reason alongside
    the store checks — the pre-exit truth this reads over real HTTP."""
    status, body = ready_status(stack, port=port)
    assert status == 503, f"/ready did not 503 with a dead perpetual task marked: {status} {body}"
    assert body["status"] == "not_ready", f"/ready status was not not_ready: {body}"
    checks = body["checks"]
    key = f"perpetual_task:{task_name}"
    assert checks.get(key) == reason, f"/ready did not name {key}={reason!r}: {checks}"
    return checks


def wait_fleet_converged(stack: TaiStack, deadline: float) -> None:
    """Block until the whole expected fleet is on the bus with every serve worker's
    presence row reading ``ready`` (:func:`wait_full_census`).

    A worker writes its row ``resyncing`` before its on-ready self-resync and ``ready``
    only once that resync returns, and every hold of its reload gate its own boot makes sits
    inside that resync, so a ``ready`` row is a worker that has left its boot gate. Each worker
    publishes its own row, so the fact is read per worker, never through the shared port.

    A busless single-worker stack joins no bus — no presence keys, no on-ready
    self-resync — so HTTP health is its full readiness and this returns at once."""
    if not needs_bus(stack.config):
        return
    wait_full_census(stack, deadline)


def expected_serve_workers(stack: TaiStack) -> int:
    """How many ``serve``-kind presence rows the booted fleet registers: a
    REPLICAS stack runs one worker per app port, a MULTIWORKER master runs
    ``--workers N`` on its single port, and the embed host is one app process
    (a MULTIWORKER shape with ``workers=1``)."""
    from tai42_e2e.topology import Topology

    if stack.config.topology is Topology.REPLICAS:
        return len(stack.app_ports)
    return stack.config.workers


def serve_workers_on_port(stack: TaiStack) -> int:
    """The serve-worker count behind a single app port — the whole MULTIWORKER
    ``--workers N`` master, or the lone worker of one REPLICAS master."""
    from tai42_e2e.topology import Topology

    if stack.config.topology is Topology.REPLICAS:
        return 1
    return stack.config.workers


def expected_backend_processes(stack: TaiStack) -> list[str]:
    """The backend processes the stack runs: the backend worker, one worker per queue label and,
    on a stack with ``green_pool_workers``, one worker per green pool of the backend variant."""
    if not stack.config.run_backend:
        return []
    green_pools = stack.infra.variants.backend.green_pool_workers() if stack.config.green_pool_workers else {}
    return [
        "backend",
        *(f"backend-queue-{label}" for label in stack.config.queue_workers),
        *(f"backend-{pool}" for pool in green_pools),
    ]


def expected_fleet_present(stack: TaiStack, workers: Sequence[BusWorker]) -> bool:
    """Whether the census rows ``workers`` hold the FULL expected fleet (:func:`_fleet_shortfall`
    finds nothing missing).

    Every serve worker the topology spawns with its row reading ``ready``, plus every backend
    process by its own pid, so a fleet with a process still respawning (it has no row) or a
    serve worker still resyncing is not present however ready the other rows are.
    """
    return not _fleet_shortfall(stack, workers)


def _fleet_shortfall(stack: TaiStack, workers: Sequence[BusWorker]) -> list[str]:
    """What the census rows ``workers`` lack of the full expected fleet, one line per gap, naming
    each serve row that is not ``ready`` by its name, pid and state and each backend process
    with no row by its name and pid; empty when the full fleet is present. The backend pids are
    read under the supervisor's lock, so a respawn in progress is read once it has started its
    process."""
    gaps: list[str] = []
    serve = sorted((w for w in workers if w.kind == "serve"), key=lambda w: w.name)
    want = expected_serve_workers(stack)
    if sum(1 for w in serve if w.state == "ready") < want:
        gaps.extend(f"serve worker {w.name} (pid {w.pid}) is {w.state}" for w in serve if w.state != "ready")
        if len(serve) < want:
            gaps.append(f"{want - len(serve)} of {want} serve workers have no presence row")
    backend_pids = {w.pid for w in workers if w.kind == "backend"}
    with stack.supervisor.lock:
        for name in expected_backend_processes(stack):
            pid = stack.process(name).pid
            if pid not in backend_pids:
                gaps.append(f"backend process {name} (pid {pid}) has no presence row")
    return gaps


def wait_full_census(stack: TaiStack, deadline: float) -> None:
    """Poll the bus census until the FULL expected fleet is present (:func:`expected_fleet_present`),
    so readiness never returns on a half-formed fleet. A timeout names every worker that is
    missing or not ready, as the last census read showed it."""
    gaps: list[str] = []

    def probe() -> bool:
        early = early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"fleet census: {early}")
        gaps[:] = _fleet_shortfall(stack, stack.census())
        return not gaps

    want = " + ".join([f"{expected_serve_workers(stack)} ready serve workers", *expected_backend_processes(stack)])
    wait_for(
        probe,
        deadline=deadline,
        message=lambda: f"the worker-bus census never reached the full fleet ({want}): {'; '.join(gaps)}",
    )


def early_exit_detail(stack: TaiStack) -> str | None:
    """The first process that exited and stays down, with its log tail, or ``None``.

    A supervised process the supervisor is bringing back (:meth:`~tai42_e2e.supervisor.RespawnSupervisor.respawns`)
    is churn, not a failure."""
    with stack.supervisor.lock:
        for handle in stack.procs.values():
            if handle.is_running() or stack.supervisor.respawns(handle):
                continue
            return f"process {handle.name!r} exited early (code {handle.poll()}):\n{handle.log_tail()}"
    return None


def wait_http_ok(stack: TaiStack, url: str, deadline: float, label: str) -> None:
    def probe() -> bool:
        early = early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"{label}: {early}")
        try:
            # 503 while a worker warms is "not ready", not a failure.
            return httpx.get(url, timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    wait_for(probe, deadline=deadline, message=f"{label} never became ready at {url}")


def wait_fresh_lives(
    stack: TaiStack,
    kind: str,
    deadline: float,
    *,
    before: Mapping[str, int] | None = None,
    replaced: Collection[str] | None = None,
    count: int = 1,
) -> None:
    """Hold until ``count`` fresh READY ``kind`` lives are on the bus census and every
    replaced life is gone.

    A worker's slot name is STABLE across a restart, so a respawn reuses the same name at an
    INCREMENTED generation (or, if the old claim has not yet lapsed, the next free name) —
    never a fresh unrelated id. ``before`` is the pre-restart ``{name: generation}`` of every
    ``kind`` life; ``replaced`` names the lives the restarted process held (default: all of
    ``before``). A life is fresh when its name is absent from ``before`` or carries a higher
    generation there; a replaced life is gone when its name is absent or superseded by a
    higher generation. Keying on the generation ignores a SIGKILLed worker's corpse row — its
    presence key lingers at the OLD generation until its heartbeat TTL — and never counts a
    sibling process's untouched life (another replica's worker) as fresh. A restarted worker
    answers requests before it has claimed its slot (the claim runs on the bus subscription
    task the app spawns at the end of its startup), so its census row is the signal that it
    holds its bus identity. With no ``before`` (the boot shape) any ``count`` ready lives pass."""
    base = dict(before or {})
    gone = base.keys() if replaced is None else replaced

    def probe() -> bool:
        early = early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"{kind} census: {early}")
        rows = {w.name: w for w in stack.census() if w.kind == kind}
        old_gone = all(name not in rows or rows[name].generation > base[name] for name in gone)
        fresh = [w for w in rows.values() if w.state == "ready" and (w.name not in base or w.generation > base[w.name])]
        return old_gone and len(fresh) >= count

    if count == 1:
        want = f"a fresh ready {kind}-kind life" if base else f"a ready {kind}-kind worker"
    else:
        want = f"{count} fresh ready {kind}-kind lives" if base else f"{count} ready {kind}-kind workers"
    wait_for(probe, deadline=deadline, message=f"{want} never appeared in the worker-bus census")
