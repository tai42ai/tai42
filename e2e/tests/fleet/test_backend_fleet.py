"""Backend fleet: a task-runtime worker rides the app-owned worker bus alongside the
HTTP serve workers, so a fleet op reaches it under load, a TARGETED op narrows delivery
to exactly the named worker kind, and an unknown target is a loud, NAMED rejection.

The stack this module boots is a MULTIWORKER(2)+backend profile (``build_core_stack``)
with one extra ingredient: a manifest ``mcp`` server — the harness's self-contained
managed MCP fixture (``tai42_e2e_fixtures.managed_mcp_server``) spawned as a stdio child
in EVERY worker (both serve workers and the backend runtime). Its ``ping`` tool binds
as ``fleetmcp_ping`` on each worker's live registry, which gives these scenarios a
detachable tool whose presence/absence is observable two ways, matched to worker kind:

* SERVE workers answer the HTTP ``e2e_worker_info`` probe, so their live view collapses
  to the harness's probe-computed ``state_digest`` — a targeted detach on one serve
  origin moves ONLY that origin's digest.
* the BACKEND worker answers no HTTP probe (the digest never observes it), so its live
  registry is read back through a REAL task it runs: ``run_tool_sync_task`` dispatches a
  tool by name INSIDE the backend worker, and a detached tool is one the backend can no
  longer dispatch.

The stack sizes ``TAI_BUS_APPLY_TIMEOUT`` (:data:`_APPLY_TIMEOUT`) well ABOVE the
in-flight fixture task's runtime (:data:`_TASK_SECONDS`) — the under-load scenario fires
a fleet op while a task blocks a backend worker, and the apply report must not cut to
``missing`` before the backend confirms.

Opt-in via ``TAI_E2E_FLEET``; runs on the selected backend leg (``TAI_E2E_BACKEND``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx
import pytest
from fastmcp.client.client import CallToolResult

from tai42_e2e import wait_for_async
from tai42_e2e.booting import boot_stack
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import build_core_stack
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack, _probe_tolerating_reloading
from tai42_e2e.variants import BusOrigin

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The seeded managed MCP server and the tool it contributes to every worker's live
# registry. ``ping`` binds under the ``<title>_`` prefix (``normalized_name``), so the
# detach target is ``fleetmcp_ping``.
_MCP_TITLE = "fleetmcp"
_MCP_TOOL = "fleetmcp_ping"

# The in-flight task runtime and the bus apply deadline sized above it. The deadline
# must exceed the task runtime so a fleet op fired mid-task still observes the backend
# worker confirming ``applied`` rather than the report cutting to ``missing``.
_TASK_SECONDS = 4.0
_APPLY_TIMEOUT = "20"


def _build_fleet_mcp_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The MULTIWORKER(2)+backend core profile plus a seeded stdio ``mcp`` server and a
    bus apply deadline sized above the in-flight task runtime.

    The managed server is launched as ``<sut-python> -m
    tai42_e2e_fixtures.managed_mcp_server`` — no network, no package index: the SUT venv
    (this process's interpreter) already has ``tai42_e2e_fixtures`` importable, so
    ``sys.executable`` is a valid absolute command regardless of the child ``PATH``.
    Every worker (both serve workers and the backend runtime) probes and binds it at
    boot, so ``fleetmcp_ping`` is live fleet-wide before the first assertion."""
    base = build_core_stack(res, variants)
    manifest = {
        **base.manifest,
        "mcp": [
            {
                "title": _MCP_TITLE,
                "config": {"command": sys.executable, "args": ["-m", "tai42_e2e_fixtures.managed_mcp_server"]},
            }
        ],
    }
    env = {**base.env, "TAI_BUS_APPLY_TIMEOUT": _APPLY_TIMEOUT}
    return replace(base, name="fleet-mcp", manifest=manifest, env=env)


@pytest.fixture(scope="module")
def fleet_mcp_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The backend-fleet stack, booted once for the module. Each scenario normalizes
    the runtime detach state at its start (see :func:`_normalize`), so the shared stack
    carries no ordering coupling between the scenarios."""
    yield from boot_stack(infra, tmp_path_factory.mktemp("fleet_mcp"), _build_fleet_mcp_stack)


# ---- census + fleet-op helpers ------------------------------------------


async def _await_fleet(stack: TaiStack) -> tuple[list[BusOrigin], BusOrigin]:
    """Poll the bus presence census until it shows the full expected fleet — the two
    serve workers and the one backend runtime — and return ``(serve_origins, backend)``.

    The census is a point-in-time scan of the presence keys and is eventually
    consistent: a whole-fleet reload (:func:`_normalize`) momentarily churns every
    worker's presence, so a single sample can catch an empty or partial fleet. Polling
    to the settled shape is the sanctioned discipline (the bus-outage recovery path
    polls the same census the same way)."""

    async def probe() -> tuple[list[BusOrigin], BusOrigin] | None:
        origins = stack.census()
        serve = [origin for origin in origins if origin.kind == "serve"]
        backend = [origin for origin in origins if origin.kind == "backend"]
        return (serve, backend[0]) if len(serve) == 2 and len(backend) == 1 else None

    return await wait_for_async(
        probe, deadline=20.0, message="the census never showed the full fleet (2 serve workers + 1 backend)"
    )


async def _post_past_reload_gate(api: ApiClient, path: str, targets: list[str] | None) -> httpx.Response:
    """POST a reload-gated fleet door, polling past the boot-time / in-flight reload
    gate's retriable ``503`` so the returned response is the door's real verdict."""

    async def settled() -> httpx.Response | None:
        resp = await api.request_raw("POST", path, json={"targets": targets})
        return resp if resp.status_code != 503 else None

    return await wait_for_async(settled, deadline=15.0, message=f"{path} never left the reload gate")


async def _fleet_reload(api: ApiClient, targets: list[str] | None = None) -> httpx.Response:
    return await _post_past_reload_gate(api, "/api/fleet/reload-config", targets)


async def _deregister_mcp(api: ApiClient, title: str, targets: list[str] | None) -> httpx.Response:
    return await _post_past_reload_gate(api, f"/api/mcp-status/{title}/deregister", targets)


async def _stable_serve_digests(stack: TaiStack, n: int) -> dict[int, str]:
    """Poll the serve fleet until a ``{pid: digest}`` snapshot of all ``n`` serve
    workers REPEATS unchanged (each worker settled, none mid-reload), then return it.

    Cross-worker digest EQUALITY is deliberately NOT required: the probe digest hashes
    each process's own live view, which is stable WITHIN a process but not identical
    ACROSS processes once the manifest carries an ``mcp`` server (a live-manifest field
    serializes per-process). The targeted narrowing is a per-worker delta — each worker
    against its own baseline — so a stable snapshot is the right baseline, not a
    converged one."""

    async def settled() -> dict[int, str] | None:
        first = await stack.wait_workers(n)
        second = await stack.wait_workers(n)
        return first if len(first) == n and first == second else None

    message = f"the {n} serve workers never settled to a stable per-worker digest snapshot"
    return await wait_for_async(settled, deadline=20.0, message=message)


async def _backend_dispatch(stack: TaiStack, tool: str, arguments: dict) -> CallToolResult:
    """Run ``tool`` INSIDE the backend worker via the ``run_tool_sync_task`` door (the
    task-execution readback) and return the raw MCP result — ``is_error`` when the
    backend can no longer dispatch the name.

    A whole-fleet reload (``_normalize``) or a targeted op settling on the serve worker
    this dispatch enters can swap that worker's serving epoch and retire the freshly-opened
    MCP session (D13a — the new epoch serves a fresh session-id space -> "Session
    terminated"). Re-open on a fresh session until the dispatch lands, exactly as a real
    client re-initialises; a genuine ``is_error`` dispatch result (the backend lost the
    tool) is a delivered result and passes straight through."""

    async def _thunk() -> CallToolResult:
        async with stack.mcp() as mcp:
            return await mcp.call_tool(
                "run_tool_sync_task",
                {"tool_name": tool, "arguments": arguments},
                raise_on_error=False,
                retry_on_reloading=True,
            )

    return await wait_for_async(
        lambda: _probe_tolerating_reloading(_thunk),
        deadline=15.0,
        message=f"the backend dispatch of {tool!r} never left the reload/session-swap window",
    )


async def _normalize(stack: TaiStack) -> None:
    """Rebind ``fleetmcp`` on every worker via a whole-fleet reload, so each scenario
    starts from the seeded state regardless of a prior scenario's targeted detach (a
    detach is runtime-only; the reload re-reads the persisted manifest that carries the
    server)."""
    resp = await _fleet_reload(stack.api(), None)
    assert resp.status_code == 200, resp.text


# ---- delivery under load ------------------------------------------------


async def test_fleet_op_reaches_backend_worker_under_load(
    fleet_mcp_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A fleet op reaches the backend-runtime worker WHILE a task is executing.

    A long-ish fixture task is started in the backend worker (observably in-flight via
    its ``started`` probe record, whose pid is NOT a serve worker's), then a whole-fleet
    ``reload-config`` is fired. Despite the in-flight task, the backend origin confirms
    ``applied`` in the fleet report — the black-box proof of under-load delivery. The
    apply deadline is sized above the task runtime so the report is not cut before the
    backend answers."""
    stack = fleet_mcp_stack
    await _normalize(stack)

    serve, backend = await _await_fleet(stack)
    serve_pids = {origin.pid for origin in serve}
    key = uniq("c1_slow")

    async def run_slow() -> None:
        # ``e2e_slow_task_sync_task`` RPUSHes a ``started`` marker then blocks IN the
        # backend worker — the record-then-block probe purpose-built for in-flight work.
        async with stack.mcp() as mcp:
            await mcp.call_tool(
                "e2e_slow_task_sync_task", {"key": key, "seconds": _TASK_SECONDS}, retry_on_reloading=True
            )

    task = asyncio.create_task(run_slow())
    try:
        # The task is genuinely in-flight in the backend fleet: its started marker
        # exists and was written by a NON-serve process.
        async def started() -> bool:
            return bool(stack.records(key))

        await wait_for_async(started, deadline=15.0, message="the backend task never reported it was in-flight")
        started_pid = json.loads(stack.records(key)[0])["pid"]
        assert started_pid not in serve_pids, f"the in-flight task ran on a serve worker ({started_pid}), not a backend"
        assert not task.done(), "the fixture task completed before the fleet op could be fired mid-flight"

        # Fire the fleet op WHILE the task blocks a backend worker; the backend applies.
        resp = await _fleet_reload(stack.api(), None)
        assert resp.status_code == 200, resp.text
        result = resp.json()["data"]
        outcomes = {r["origin"]: r["outcome"] for r in result["results"]}
        assert outcomes.get(backend.origin) == "applied", f"the backend did not apply under load: {result}"
    finally:
        # Drain the in-flight task. Its own SURVIVAL is not what this test asserts (delivery of
        # the op to the busy backend is) — a config reload reinitializes the backend
        # runtime and may drop the in-flight job's result, which is a legitimate outcome
        # of reloading under load, not a delivery failure. Suppress its error either way.
        with contextlib.suppress(Exception):
            await task


# ---- narrowing among SERVE workers --------------------------------------


async def test_targeted_deregister_narrows_to_one_serve_worker(fleet_mcp_stack: TaiStack) -> None:
    """A TARGETED ``deregister_mcp`` of ONE serve origin moves ONLY that worker's
    digest; every other serve digest is byte-identical.

    The detach is the point: a targeted re-probe of unchanged config would leave the
    digests identical, so the assertion needs a real change on the target and no change
    anywhere else."""
    stack = fleet_mcp_stack
    await _normalize(stack)

    serve, _ = await _await_fleet(stack)
    target, other = serve[0], serve[1]

    baseline = await _stable_serve_digests(stack, 2)
    base_target = baseline[target.pid]
    base_other = baseline[other.pid]

    # Detach fleetmcp on the target serve origin only.
    resp = await _deregister_mcp(stack.api(), _MCP_TITLE, [target.origin])
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]
    outcomes = {r["origin"]: r["outcome"] for r in result["results"]}
    assert outcomes.get(target.origin) == "applied", f"the targeted serve origin did not apply: {result}"
    # The report named ONLY the target — no other origin was delivered to.
    assert set(outcomes) == {target.origin}, f"a targeted detach reached non-target origins: {result}"

    # The target's digest moves off its own baseline; the other serve worker's digest
    # holds at its own baseline (each worker compared against itself — see
    # :func:`_stable_serve_digests`).
    async def target_changed() -> bool:
        seen = await stack.wait_workers(2)
        return seen.get(target.pid) not in (None, base_target)

    await wait_for_async(target_changed, deadline=15.0, message="the targeted serve worker's digest never changed")
    after = await stack.wait_workers(2)
    assert after[target.pid] != base_target, "the targeted serve worker's digest did not change"
    assert after[other.pid] == base_other, f"an untargeted serve worker's digest changed: {after} vs {baseline}"


# ---- delivery TO a backend worker ---------------------------------------


async def test_targeted_deregister_reaches_the_backend_worker(fleet_mcp_stack: TaiStack, infra: Infra) -> None:
    """A TARGETED ``deregister_mcp`` of the BACKEND origin removes the detached tool
    from the backend worker's live registry, read back through a REAL task the backend
    runs (the digest never observes backend workers). The serve workers, not targeted,
    keep serving the tool.

    Celery's backend runtime is a PREFORK pool: the managed stdio MCP server's live
    subprocess connection does not survive the fork into the pool child, so
    ``fleetmcp_ping`` is ENTIRELY ABSENT from the child's live registry — a
    ``list_tools`` readback run INSIDE the backend never lists it and a dispatch
    answers ``No such tool: fleetmcp_ping`` (whereas every serve worker still binds
    and serves it). The observable here is a detach moving the tool ``present -> absent``
    in the backend's OWN registry; with the tool never present in the prefork child,
    that observable does not exist on celery, so the celery leg is skipped. The arq/rq
    fork models keep the tool bound and dispatchable in the runtime, so it runs there."""
    if infra.variants.backend.name == "celery":
        pytest.skip(
            "managed stdio MCP in a celery prefork child: fleetmcp_ping is entirely absent from the backend "
            "prefork child's live registry (not listed by a backend-run list_tools, dispatch answers 'No such "
            "tool') because the stdio subprocess connection does not survive the fork, so this present->absent "
            "deregister observable cannot be read back on celery. arq/rq keep the tool bound and stay covered."
        )
    stack = fleet_mcp_stack
    await _normalize(stack)

    _, backend = await _await_fleet(stack)

    # Precondition: the backend worker dispatches fleetmcp_ping (the tool is bound).
    before = await _backend_dispatch(stack, _MCP_TOOL, {})
    assert not before.is_error, f"the backend could not dispatch {_MCP_TOOL} before the detach: {before}"

    # Detach fleetmcp on the backend origin only.
    resp = await _deregister_mcp(stack.api(), _MCP_TITLE, [backend.origin])
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]
    outcomes = {r["origin"]: r["outcome"] for r in result["results"]}
    assert outcomes.get(backend.origin) == "applied", f"the backend origin did not apply the detach: {result}"
    assert set(outcomes) == {backend.origin}, f"a backend-targeted detach reached other origins: {result}"

    # Task-execution readback: the backend can no longer dispatch the detached tool.
    async def backend_lost_tool() -> bool:
        result = await _backend_dispatch(stack, _MCP_TOOL, {})
        return result.is_error

    await wait_for_async(
        backend_lost_tool, deadline=15.0, message="the backend worker never lost the detached tool from its registry"
    )

    # The serve workers were not targeted, so they still serve the tool.
    async with stack.mcp() as mcp:
        serve_tools = await mcp.tool_names()
    assert _MCP_TOOL in serve_tools, f"a serve worker lost a tool the detach targeted only at the backend: {_MCP_TOOL}"


# ---- unknown-target rejection -------------------------------------------


async def test_publish_naming_unknown_target_is_a_named_error(fleet_mcp_stack: TaiStack) -> None:
    """A publish naming a NONEXISTENT target is rejected as a client error whose body
    NAMES the unknown target — the ``validate_targets`` raise, fired BEFORE any local
    side effect (no silent narrowing to the live fleet)."""
    stack = fleet_mcp_stack
    await _normalize(stack)
    # A settled fleet, so the rejection is specifically about the bogus target.
    await _await_fleet(stack)

    bogus = "backend-no-such-worker-99999"
    resp = await _deregister_mcp(stack.api(), _MCP_TITLE, [bogus])
    assert resp.status_code == 400, resp.text
    assert bogus in resp.text, resp.text
    assert "unknown fleet targets" in resp.text, resp.text
