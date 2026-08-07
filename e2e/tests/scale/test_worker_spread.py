"""Correctness under width, past the usual A/B ceiling. This is a
correctness-under-width probe, NOT a perf/soak suite: no timing budgets, no
sustained load beyond what a bounded spread check needs, no xdist (those stay out
of scope).

Two properties, each on the topology that can honestly show it:

* WIDTH — a ``workers=4`` MULTIWORKER stack serves bursts of ≥32 in-flight tool
  calls with ZERO failures and spreads the work across at least 3 distinct workers.
  A single burst's spread is at the mercy of the kernel's accept scheduling, so the
  ≥3-worker spread is accumulated across back-to-back full-width bursts within a
  bounded window — the pool is genuinely parallel and reaches ≥3 workers under
  sustained width, not that one burst fans out perfectly. Driven over ``POST
  /api/run-tool`` (a stateless request that legitimately spreads across the pool);
  the MCP transport is stateful-per-session, which the width property does not need.

* SUB-MCP MOUNT — a sub-MCP registered at runtime serves over the REAL MCP surface.
  This runs on a STATEFUL SINGLE-WORKER stack: a live ``StreamableHTTPServerTransport``
  session is not shareable across a stateless-http multiworker pool (that boundary is
  enforced at boot and covered elsewhere), so the honest place to exercise the mount
  end-to-end over MCP is one stateful worker. Nothing is swallowed — a mount that does
  not serve raises."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import build_core_stack
from tai42_e2e.stack import StackConfig, StackResources, TaiStack, _probe_tolerating_reloading
from tai42_e2e.variants import Variants

_WORKERS = 4
_BURST = 36  # ≥32 in-flight per burst.


def _scale_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A core stack widened to four uvicorn workers (stateless-http kicks in above
    one worker, so requests spread across the pool)."""
    return replace(build_core_stack(res, variants), workers=_WORKERS)


def _stateful_single_worker_stack(res: StackResources, variants: Variants) -> StackConfig:
    """A one-worker core stack: below the multiworker threshold, so the MCP transport
    stays STATEFUL (no ``--stateless-http``) and a sub-MCP session works end to end."""
    return replace(build_core_stack(res, variants), workers=1)


async def _drive_burst(stack: TaiStack, key: str, n: int) -> None:
    """Fire ``n`` concurrent ``POST /api/run-tool`` calls to ``e2e_record``, each
    on its own connection: the probe records the serving worker's pid onto the
    channel ``key``. gather without ``return_exceptions`` so ANY failed call
    raises — the zero-failures-under-width invariant."""
    api = stack.api()
    await asyncio.gather(
        *(
            api.post("/api/run-tool", json={"tool_name": "e2e_record", "arguments": {"key": key, "value": "w"}})
            for _ in range(n)
        )
    )


def _worker_pids(stack: TaiStack, key: str) -> set[int]:
    """The distinct worker pids that recorded onto the probe channel ``key``."""
    return {int(json.loads(record)["pid"]) for record in stack.records(key)}


async def test_burst_spreads_across_four_workers_with_zero_failures(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(_scale_stack)
    key = uniq("spread")

    # Back-to-back full-width bursts (each ≥32 in-flight, each raising on any failed
    # call — the zero-failures-under-width invariant) until at least 3 distinct
    # workers have served, bounded.
    async def spread_reached() -> bool:
        await _drive_burst(stack, key, _BURST)
        return len(_worker_pids(stack, key)) >= 3

    await wait_for_async(
        spread_reached,
        deadline=30.0,
        interval=0.0,
        message=f"≥32-in-flight bursts never spread across ≥3 workers (saw {_worker_pids(stack, key)})",
    )


async def test_sub_mcp_registered_at_runtime_serves_over_the_mcp_surface(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(_stateful_single_worker_stack)
    slug = uniq("scale").replace("_", "-")
    mount_key = uniq("mount")

    # Register a sub-MCP at runtime, then drive it over the REAL MCP surface: the
    # mount's probe tool must run and record. Nothing is swallowed — a mount that the
    # registry never built raises here (an unknown-tool / unmounted-slug error), never
    # a silent "not yet".
    await stack.api().post("/api/sub-mcp", json={"slug": slug, "tools": ["e2e_record"]})

    async def mount_serves() -> bool:
        # GET /api/sub-mcp unwraps to a {slug: config} mapping.
        if slug not in await stack.api().get("/api/sub-mcp"):
            return False

        # The sub-MCP registration reloaded the worker, swapping its serving epoch; a
        # session opened on the mount can be retired mid-listing (D13a — the new epoch
        # serves a fresh session-id space). Tolerate it so this wait re-polls on a fresh
        # session, exactly as a real client re-initialises.
        async def _thunk() -> bool:
            async with stack.mcp(path=f"/app/{slug}") as mcp:
                return "e2e_record" in await mcp.tool_names()

        return bool(await _probe_tolerating_reloading(_thunk))

    await wait_for_async(mount_serves, deadline=10.0, message=f"sub-MCP {slug} never served over the MCP surface")

    before = len(stack.records(mount_key))
    async with stack.mcp(path=f"/app/{slug}") as mcp:
        await mcp.call_tool("e2e_record", {"key": mount_key, "value": "mounted"})
    assert len(stack.records(mount_key)) > before, f"the sub-MCP mount {slug} served no probe record"
