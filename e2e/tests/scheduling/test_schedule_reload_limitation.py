"""PINNED KNOWN LIMITATION — a celery prefork worker can stop dispatching under a
``schedule_task``-branched tool that also rides heavy reload churn.

A live config reload restarts a prefork worker's pool (soft ``pool_restart``) so its
children re-inherit the updated tool registry. Under the accumulated restarts of the
``reload_config`` + preset ``reload_tool`` churn this spec drives, WITH a
``schedule_task`` branch on the shared worker, celery's prefork pool can re-fork
cleanly yet leave the parent unable to dispatch to the fresh child — every subsequent
backend task on that worker then hangs. The reload's ``stats`` turnover confirmation
cannot see it: the pool HAS re-forked and is at full size. The fix is an architecture
change (a non-forking pool, or a full worker-process respawn on reload), tracked
out-of-band; the SUPPORTED ``schedule_task`` + reload combination is covered
separately by ``test_schedule_task.py``.

The reproduction is timing-dependent (the branch only shifts reload timing by a few
ms, which is enough to make celery's latent restart race deterministic on some
hosts and not others), so it is SKIPPED by default — the skip reason keeps the
limitation visible in every run's report without wedging a worker in the gate. Opt in
to run it (on a host where it reproduces) with:

    TAI_E2E_PIN_LIMITATIONS=1 TAI_E2E_BACKEND=celery uv run pytest tests/scheduling/test_schedule_reload_limitation.py
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import os
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import PROBE_TOOLS_TITLE, build_replicas_stack
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants

# How long the final dispatch is given to land. A healthy pool answers in well under
# a second, so exceeding this means the pool re-forked but stopped dispatching.
_WEDGE_DEADLINE = 30.0

pytestmark = pytest.mark.skipif(
    not os.environ.get("TAI_E2E_PIN_LIMITATIONS"),
    reason="pinned celery-prefork limitation: a schedule_task-branched tool under heavy reload churn can leave the "
    "worker's re-forked pool unable to dispatch (timing-dependent, tracked for an in-place-reload fix — see this "
    "module's docstring). Opt in with TAI_E2E_PIN_LIMITATIONS=1 TAI_E2E_BACKEND=celery.",
)


def _replicas_with_schedule_branch(res: StackResources, variants: Variants) -> StackConfig:
    """The reload-heavy ``replicas`` profile with the ``schedule_task`` branch
    injected onto ``e2e_record`` — the manifest shape that puts a schedule-bearing
    tool on a worker also taking the full preset reload churn."""
    config = build_replicas_stack(res, variants)
    manifest = copy.deepcopy(config.manifest)
    for entry in manifest["tools"]:
        if entry.get("title") == PROBE_TOOLS_TITLE:
            entry.setdefault("extensions", {})["e2e_record"] = [["schedule_task"]]
    return dataclasses.replace(config, manifest=manifest)


def _tool_text(result: Any) -> str:
    if result.data is not None:
        return str(result.data)
    if result.structured_content is not None:
        return str(result.structured_content)
    return "".join(getattr(block, "text", "") for block in (result.content or []))


def _reloading(exc: ToolError) -> bool:
    return "reloading" in str(exc)


def _session_terminated(exc: McpError) -> bool:
    """True for the D13a session-swap rejection — a worker that swaps its serving epoch
    under a fan-out reload retires the MCP session a poll opened against the old epoch, so
    the SDK raises ``McpError`` "Session terminated". A polled predicate treats it as
    "not yet" and re-polls on a fresh session. Any other MCP error propagates."""
    return "Session terminated" in str(exc)


async def _call_preset(stack: TaiStack, port: int, name: str) -> str:
    async with stack.mcp(port=port) as mcp:
        result = await mcp.call_tool(name, {})
    return _tool_text(result)


async def _backend_call(stack: TaiStack, name: str) -> str:
    async with stack.mcp() as mcp:
        result = await mcp.call_tool("run_tool_sync_task", {"tool_name": name, "arguments": {}})
    return _tool_text(result)


async def test_schedule_branch_plus_reload_churn_wedges_celery_prefork(
    fresh_stack: Callable[..., TaiStack], infra: Infra, uniq: Callable[[str], str]
) -> None:
    if infra.variants.backend.name != "celery":
        pytest.skip("the prefork-restart limitation is specific to the celery backend")

    stack = fresh_stack(_replicas_with_schedule_branch)
    api_a = stack.api(port=stack.port_a)
    api_b = stack.api(port=stack.port_b)

    # PRIME: a preset create + a reload_config fanout that re-imports the backend and
    # restarts the pool.
    prime = uniq("preset")
    await api_a.post(
        "/api/presets",
        json={
            "name": prime,
            "base_tool": "e2e_echo",
            "description": "prime echo preset",
            "fixed_kwargs": {"payload": "baked"},
        },
    )
    await api_a.post("/api/config/env", json={})

    # CHURN: create + rebind + re-version + rollback, each fanning a reload_tool pool
    # restart, interleaved with backend dispatches into the pool.
    name = uniq("preset")
    baked = uniq("payload")
    created = await api_a.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "churn echo preset",
            "fixed_kwargs": {"payload": baked},
        },
    )
    original_version = created["active_version"]
    assert await _call_preset(stack, stack.port_a, name) == baked

    await api_b.post("/api/config/env", json={})

    async def b_serves_baked() -> bool:
        try:
            async with stack.mcp(port=stack.port_b) as mcp:
                if name not in await mcp.tool_names():
                    return False
            return await _call_preset(stack, stack.port_b, name) == baked
        except ToolError as exc:
            if _reloading(exc):
                return False
            raise
        except McpError as exc:
            if _session_terminated(exc):
                return False
            raise

    await wait_for_async(b_serves_baked, deadline=5.0, message="B never rebound the preset after its reload")

    new_payload = uniq("payload")
    await api_b.post(f"/api/presets/{name}/versions", json={"fixed_kwargs": {"payload": new_payload}})

    async def backend_sees_new() -> bool:
        try:
            return await _backend_call(stack, name) == new_payload
        except ToolError as exc:
            if _reloading(exc):
                return False
            raise
        except McpError as exc:
            if _session_terminated(exc):
                return False
            raise

    await wait_for_async(backend_sees_new, deadline=5.0, message="backend worker never saw the new preset version")

    await api_b.post(f"/api/presets/{name}/rollback", json={"version": original_version})
    # The pinned limitation: on a host where it reproduces, the re-forked pool no longer
    # dispatches, so this final backend call never lands within the wedge deadline. We must
    # NOT assert it hangs: the reproduction is host-timing-dependent (see the module
    # docstring), so on a host that does not reproduce it the call returns promptly — which
    # is NOT a regression and must never fail the ``-x`` gate. Distinguish the two outcomes:
    #  * hang (TimeoutError) → the limitation is still present as pinned; the tripwire holds
    #    and the test passes.
    #  * lands in time → the wedge did not reproduce on this host/run; skip LOUDLY rather than
    #    assert, so a genuine fix surfaces as a persistent, reasoned skip (prompting pin
    #    removal) instead of a false weekly RED.
    try:
        await asyncio.wait_for(_backend_call(stack, name), timeout=_WEDGE_DEADLINE)
    except TimeoutError:
        return
    pytest.skip(
        "celery-prefork wedge did NOT reproduce on this host/run: the final backend dispatch "
        "landed within the wedge deadline. The reproduction is host-timing-dependent, so a single "
        "non-repro is not proof of a fix — but if it stops reproducing consistently, the pinned "
        "limitation may be FIXED and this pin should be removed (see this module's docstring)."
    )
