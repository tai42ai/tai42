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
from tai42_e2e.waiting import wait_for, wait_for_async

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

    from tai42_e2e.stack import TaiStack


def wait_ready(stack: TaiStack) -> None:
    deadline = stack.infra.settings.boot_timeout
    for port in stack.app_ports:
        wait_http_ok(stack, f"http://{stack.host}:{port}/health", deadline, "app health")
    if stack.config.run_metrics:
        assert stack.metrics_port is not None
        wait_http_ok(stack, f"http://{stack.host}:{stack.metrics_port}/metrics", deadline, "metrics")
    wait_fleet_converged(stack, deadline)


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
    serve worker the topology spawns plus the backend worker when a backend is
    registered — so readiness never returns on a half-formed fleet."""
    expected_serve = expected_serve_workers(stack)

    def probe() -> bool:
        early = early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"fleet census: {early}")
        workers = stack.census()
        if sum(1 for w in workers if w.kind == "serve") < expected_serve:
            return False
        return not (stack.config.run_backend and not any(w.kind == "backend" for w in workers))

    want = f"{expected_serve} serve workers" + (" + backend" if stack.config.run_backend else "")
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
    async with stack.mcp(auth=stack.auth_token) as client:
        if "e2e_worker_info" not in await client.tool_names():
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


def early_exit_detail(stack: TaiStack) -> str | None:
    for handle in stack._procs.values():
        if not handle.is_running():
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


def wait_backend_census(stack: TaiStack, deadline: float, baseline: Mapping[str, int] | None = None) -> None:
    # The bus census lists ALL workers (serve + backend) by slot name, so a restart
    # keys on a fresh ``backend``-kind LIFE: a worker's slot name is STABLE across a
    # restart, so a respawn reuses the same name at an INCREMENTED generation (or, if
    # the old claim has not yet lapsed, the next free name) — never a fresh unrelated
    # id. ``baseline`` is the pre-restart ``{name: generation}`` of the kind; the wait
    # holds until BOTH facts land: every baseline life is GONE (its name absent, or a
    # higher generation on that name) AND at least one fresh READY life of the kind
    # exists (a name absent from the baseline, or a higher generation on a baseline
    # name). Keying on the generation ignores a SIGKILLed worker's corpse row — its
    # presence key lingers at the OLD generation until its heartbeat TTL — so the wait
    # never passes on the corpse before the replacement has joined and gone ready.
    base = dict(baseline or {})

    def probe() -> bool:
        early = early_exit_detail(stack)
        if early is not None:
            raise RuntimeError(f"backend census: {early}")
        rows = {w.name: w for w in stack.census() if w.kind == "backend"}
        old_gone = all(name not in rows or rows[name].generation > gen for name, gen in base.items())
        fresh_ready = any(
            w.state == "ready" and (w.name not in base or w.generation > base[w.name]) for w in rows.values()
        )
        return old_gone and fresh_ready

    want = "a fresh ready backend-kind life" if base else "a ready backend-kind worker"
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
        data = await _probe_tolerating_reloading(worker_info)
        if data is None:
            return False
        seen[int(data["pid"])] = str(data["state_digest"])
        return len(seen) >= n

    await wait_for_async(probe, deadline=deadline, message=f"only saw {sorted(seen)} of {n} workers")
    return seen
