"""C4 / G2 — presets are versioned-only (clean break, no ephemeral tier). A
store-backed read is coherent across replicas immediately; a live TOOL rebind on
a sibling HTTP worker follows only after that worker's own reload, while the
backend worker gets the fan-out."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


def _session_terminated(exc: McpError) -> bool:
    """True for the D13a session-swap rejection.

    A sibling/backend worker applying a fan-out reload swaps its serving epoch, retiring
    the MCP session a poll opened against the old epoch (the new epoch serves a fresh
    session-id space); the SDK raises ``McpError`` "Session terminated". A polled
    predicate treats it as "not yet" and re-polls on a fresh session — exactly as a real
    client re-initialises. Any other MCP error is real and propagates."""
    return "Session terminated" in str(exc)


def _tool_text(result: Any) -> str:
    """Return a tool call's scalar payload as text.

    A tool that declares structured output surfaces its value on ``data``; the
    generic ``run_tool_sync_task`` door has no static output schema, so a scalar
    payload arrives as plain text content instead. Read whichever the call
    produced so both the direct-tool and backend-door paths compare equal."""
    if result.data is not None:
        return str(result.data)
    if result.structured_content is not None:
        return str(result.structured_content)
    return "".join(getattr(block, "text", "") for block in (result.content or []))


def _reloading(exc: ToolError) -> bool:
    """True for the worker's retriable mid-reload rejection.

    A worker applying a config reload refuses tool calls with a "reloading —
    retry shortly" error; a polled predicate treats that as "not yet" and
    polls again, exactly as a client would. Any other tool error is real and
    propagates."""
    return "reloading" in str(exc)


async def _call_preset(stack: TaiStack, port: int, name: str) -> str:
    async with stack.mcp(port=port) as mcp:
        result = await mcp.call_tool(name, {})
    return _tool_text(result)


async def _backend_call(stack: TaiStack, name: str) -> str:
    async with stack.mcp() as mcp:
        result = await mcp.call_tool("run_tool_sync_task", {"tool_name": name, "arguments": {}})
    return _tool_text(result)


async def test_preset_create_always_persists(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    name = uniq("preset")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    record = await api_a.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "cross-worker echo preset",
            "fixed_kwargs": {"payload": "baked"},
        },
        retry_on_reloading=True,
    )
    # Versioned-only: no ephemeral/persisted tier keys, an int active_version.
    assert "persisted" not in record
    assert "ephemeral" not in record
    assert isinstance(record["active_version"], int)
    # Create always persists -> visible on BOTH replicas' store reads.
    names_a = [p["name"] for p in await api_a.get("/api/presets")]
    names_b = [p["name"] for p in await replicas_stack.api(port=replicas_stack.port_b).get("/api/presets")]
    assert name in names_a
    assert name in names_b
    await api_a.post("/api/config/env", json={})
    assert name in [p["name"] for p in await api_a.get("/api/presets")]


async def test_preset_created_on_a_visible_on_b_and_rebinds_on_reload(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    name = uniq("preset")
    baked = uniq("payload")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    created = await api_a.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "cross-worker echo preset",
            "fixed_kwargs": {"payload": baked},
        },
        retry_on_reloading=True,
    )
    original_version = created["active_version"]

    # Store read is coherent on B immediately; A's live binding works immediately.
    assert (await api_b.get(f"/api/presets/{name}"))["name"] == name
    assert await _call_preset(replicas_stack, replicas_stack.port_a, name) == baked

    # B's live binding follows only after B's own reload.
    await api_b.post("/api/config/env", json={})

    async def b_serves_baked() -> bool:
        try:
            async with replicas_stack.mcp(port=replicas_stack.port_b) as mcp:
                if name not in await mcp.tool_names():
                    return False
            return await _call_preset(replicas_stack, replicas_stack.port_b, name) == baked
        except ToolError as exc:
            if _reloading(exc):
                return False
            raise
        except McpError as exc:
            if _session_terminated(exc):
                return False
            raise

    await wait_for_async(b_serves_baked, deadline=5.0, message="B never rebound the preset after its reload")

    # New version on B: the mutating worker serves it immediately; the backend
    # worker got the reload_tool fan-out.
    new_payload = uniq("payload")
    await api_b.post(f"/api/presets/{name}/versions", json={"fixed_kwargs": {"payload": new_payload}})
    assert await _call_preset(replicas_stack, replicas_stack.port_b, name) == new_payload

    async def backend_sees_new() -> bool:
        try:
            return await _backend_call(replicas_stack, name) == new_payload
        except ToolError as exc:
            if _reloading(exc):
                return False
            raise
        except McpError as exc:
            if _session_terminated(exc):
                return False
            raise

    await wait_for_async(backend_sees_new, deadline=5.0, message="backend worker never saw the new preset version")

    # Rollback on B to the original version: the mutating worker (B) serves the
    # old baked value immediately, and the backend worker gets the fan-out.
    await api_b.post(f"/api/presets/{name}/rollback", json={"version": original_version})
    assert await _call_preset(replicas_stack, replicas_stack.port_b, name) == baked

    async def backend_sees_old() -> bool:
        try:
            return await _backend_call(replicas_stack, name) == baked
        except ToolError as exc:
            if _reloading(exc):
                return False
            raise
        except McpError as exc:
            if _session_terminated(exc):
                return False
            raise

    await wait_for_async(backend_sees_old, deadline=5.0, message="backend worker never saw the rolled-back preset")
