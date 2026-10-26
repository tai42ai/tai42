"""Readiness waits for the boot engine: HTTP health, the worker-bus census, and
draining the boot-time self-resync reload gate fleet-wide, so a test acting the
instant the stack fixture returns never races an incomplete fleet or a held gate."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

import httpx

from tai42_e2e.binaries import _probe_tolerating_reloading
from tai42_e2e.child_env import needs_bus
from tai42_e2e.waiting import WaitTimeoutError, wait_for, wait_for_async

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping

    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.stack import TaiStack


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
    """Block until the whole expected fleet is on the bus AND every serve worker
    has left its boot-time self-resync reload gate, so a test acting the instant
    the stack fixture returns never races an incomplete fleet or a still-held gate.

    A busless single-worker stack joins no bus — no presence keys, no on-ready
    self-resync — so HTTP health is its full readiness and this returns at once."""
    if not needs_bus(stack.config):
        return
    wait_full_census(stack, deadline)
    drain_boot_gate(stack, deadline)


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


def wait_full_census(stack: TaiStack, deadline: float) -> None:
    """Poll the bus census until the FULL expected fleet is present — every
    serve worker the topology spawns plus, when a backend is registered, the backend
    worker and every queue-label worker, each by its own pid — so readiness never
    returns on a half-formed fleet."""
    expected_serve = expected_serve_workers(stack)
    backend_procs = (
        ["backend", *(f"backend-queue-{label}" for label in stack.config.queue_workers)]
        if stack.config.run_backend
        else []
    )

    def probe() -> bool:
        early = early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"fleet census: {early}")
        workers = stack.census()
        if sum(1 for w in workers if w.kind == "serve") < expected_serve:
            return False
        backend_pids = {w.pid for w in workers if w.kind == "backend"}
        return all(stack.process(name).pid in backend_pids for name in backend_procs)

    want = " + ".join([f"{expected_serve} serve workers", *backend_procs])
    wait_for(probe, deadline=deadline, message=f"the worker-bus census never reached the full fleet ({want})")


def drain_boot_gate(stack: TaiStack, deadline: float) -> None:
    """Drive a gated probe on every app port until it answers non-``reloading``,
    so the boot-time self-resync gate has cleared fleet-wide before the fixture
    returns. A single-port MULTIWORKER master spreads stateless requests across its
    workers, so the probe runs until every worker pid has answered clear; a
    REPLICAS port owns one worker, so one clear answer per port suffices."""
    run_readiness_coro(drain_gate_coro(stack, stack.app_ports, deadline))


async def drain_gate_coro(stack: TaiStack, app_ports: list[int], deadline: float) -> None:
    # A profile that carries no ``e2e_worker_info`` probe never fires an immediate
    # gated tool call in its tests, so its gate needs no draining here — the
    # full-census wait is that profile's convergence.
    names: list[str] = []

    async def list_tools() -> list[str]:
        async with stack.mcp(auth=stack.auth_token) as client:
            return await client.tool_names()

    async def listed() -> bool:
        got = await _probe_tolerating_reloading(
            lambda: _unless_its_process_exits(stack, stack.app_ports[0], list_tools)
        )
        if got is None:
            return False
        names.extend(got)
        return True

    await wait_for_async(listed, deadline=deadline, message="the serve fleet never answered its tool list")
    if "e2e_worker_info" not in names:
        return
    # Positive confirmation the boot self-resync gate has cleared: each serve worker
    # answers a real ``e2e_worker_info`` call non-``reloading`` before the fixture
    # returns, so a test firing a gated request the instant the stack is ready never
    # races the gate.
    workers = serve_workers_on_port(stack)
    # The budget must model the stack: N workers each import the whole platform
    # and hold their own ~2s self-resync gate, and on a contended CI runner the
    # flat boot budget can elapse before even ONE of a wide pool answers (the
    # observed "only saw [] of 4 workers" flake). Scale the drain per worker;
    # a healthy narrow stack still clears in a fraction of it.
    for port in app_ports:
        await stack.wait_workers(workers, port=port, deadline=deadline * max(1, workers))


async def _unless_its_process_exits[T](stack: TaiStack, port: int, call: Callable[[], Awaitable[T]]) -> T | None:
    """Await one probe ``call`` against the process serving ``port``, or ``None`` when that
    process is not up to answer it.

    A supervised worker can be recycled while the fleet boots. A server that stops
    mid-request never answers the request it held, and the MCP client would wait out its
    whole read bound, so the call is cancelled the moment its process exits; a respawned
    process is polled only once its ``/health`` answers. A graceful stop closes the
    listener before the process exits, so a call that fails while its process still runs
    counts as the process going away when it stops answering ``/health``
    (:func:`_going_away`); a call that fails while its process keeps answering raises.
    Either way the enclosing wait polls again. A process that exited and stays down raises
    its early-exit detail."""
    handle = stack.process_on_port(port)
    if not await _health_ok(stack, port):
        _raise_on_early_exit(stack, port)
        return None
    task = asyncio.ensure_future(call())
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        if done:
            if task.exception() is not None and await _going_away(stack, port, handle):
                # The call failed because its process went away under it.
                _raise_on_early_exit(stack, port)
                return None
            return task.result()
        if not handle.is_running():
            task.cancel()
            await asyncio.wait({task})
            _raise_on_early_exit(stack, port)
            return None


# How long a probe call that failed waits for its still-running process to show it is going
# away. A server stopping gracefully (uvicorn on SIGTERM) closes its listener on its main
# loop's next tick, a tenth of a second, and drains before the process exits; the window
# covers that tick many times over.
_GOING_AWAY_WINDOW = 1.0


async def _going_away(stack: TaiStack, port: int, handle: ProcessHandle) -> bool:
    """Whether the process ``handle`` a probe call failed against is going away.

    It is when it has exited or, within :data:`_GOING_AWAY_WINDOW`, its ``/health`` on
    ``port`` stops answering; a process that keeps answering for the whole window is up,
    so the call's failure is its own."""

    async def gone() -> bool:
        return not handle.is_running() or not await _health_ok(stack, port)

    try:
        await wait_for_async(gone, deadline=_GOING_AWAY_WINDOW, message=f"the process on port {port} stays up")
    except WaitTimeoutError:
        return False
    return True


async def _health_ok(stack: TaiStack, port: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.get(f"{stack.base_url(port)}/health")).status_code == 200
    except httpx.HTTPError:
        return False


def _raise_on_early_exit(stack: TaiStack, port: int) -> None:
    early = early_exit_detail(stack)
    if early is not None:
        raise RuntimeError(f"probe on port {port}: {early}")


def early_exit_detail(stack: TaiStack) -> str | None:
    """The first process that exited and stays down, with its log tail, or ``None``.

    A supervised process the supervisor is bringing back (``TaiStack._supervisor_respawns``)
    is churn, not a failure."""
    with stack._supervisor_lock:
        for handle in stack._procs.values():
            if handle.is_running() or stack._supervisor_respawns(handle):
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


def run_readiness_coro(coro: Coroutine[Any, Any, None]) -> None:
    """Run a readiness coroutine to completion from synchronous boot/restart code.
    Boot runs outside any event loop, but ``restart`` is called from within an
    async test's running loop, so the coroutine is driven on a dedicated thread
    with its own loop — correct whether or not the calling thread already owns one,
    and its failure is re-raised on the calling thread."""
    box: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # re-raised on the calling thread below
            box["exc"] = exc

    thread = threading.Thread(target=runner, name="tai-e2e-readiness")
    thread.start()
    thread.join()
    if "exc" in box:
        raise box["exc"]


async def wait_workers(stack: TaiStack, n: int, *, port: int | None = None, deadline: float = 10.0) -> dict[int, str]:
    """Poll the ``e2e_worker_info`` probe until ``n`` distinct worker pids have
    answered, returning each pid mapped to its reported state digest. Convergence
    assertions compare those digests: every distinct pid reporting the same digest,
    differing from the pre-mutation baseline, is fleet-wide convergence. ``port``
    selects which app port to probe (default the primary). The stack's own auth token
    authenticates the probe, so the drain reaches the fenced surface."""
    seen: dict[int, str] = {}

    async def worker_info() -> dict[str, Any]:
        async with stack.mcp(port, auth=stack.auth_token) as client:
            # A worker fresh in the census may still hold its boot-time reload gate
            # (the ~2s self-resync), so poll past the retriable ``reloading`` rejection.
            result = await client.call_tool("e2e_worker_info", retry_on_reloading=True)
        data = result.data if result.data is not None else result.structured_content
        if not isinstance(data, dict) or "pid" not in data or "state_digest" not in data:
            raise RuntimeError(f"e2e_worker_info returned an unexpected shape: {data!r}")
        return data

    async def probe() -> bool:
        # The MCP initialize handshake itself is rejected while the worker holds its
        # self-resync gate (the client raises before a session exists, so the tool
        # call's own ``retry_on_reloading`` cannot cover it); treat that envelope as
        # "not ready yet" and keep polling.
        data = await _probe_tolerating_reloading(
            lambda: _unless_its_process_exits(stack, port if port is not None else stack.app_ports[0], worker_info)
        )
        if data is None:
            return False
        seen[int(data["pid"])] = str(data["state_digest"])
        return len(seen) >= n

    await wait_for_async(probe, deadline=deadline, message=f"only saw {sorted(seen)} of {n} workers")
    return seen
