"""Shared arrange/observe helpers for the trigger-link journey suite.

The observation seam is the ``e2e_record`` side effect read back off Redis via
``stack.records(...)`` (a trigger fire is a fire-and-forget ``BackgroundTask`` with
no return channel), polled without a sleep. Every miss at the resolver door answers
the SAME uniform 404 body, so the helpers surface both the message and the raw
bytes for the byte-equality pin."""

from __future__ import annotations

import json
from typing import Any

from tai42_e2e import wait_for
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The single uniform door miss the resolver answers for EVERY cause (unknown /
# expired / revoked / tombstoned / verifier-bound). The wire body is byte-identical
# across causes; the server log distinguishes them by hash prefix, the wire never does.
MISS_MESSAGE = "unknown or expired trigger link"


def no_auth(stack: TaiStack, port: int | None = None) -> ApiClient:
    """A client carrying NO Authorization header — the public resolver / boundary
    caller. The resolver door is reachable unauthenticated; the CRUD routes deny it."""
    return ApiClient(f"http://{stack.host}:{port or stack.port_a}")


async def register_record_hook(
    admin: ApiClient,
    topic: str,
    *,
    name: str,
    execution_key: str,
    tool_kwargs: dict[str, Any] | None = None,
    expr: str | None = None,
) -> None:
    """Register an ``e2e_record`` hook on ``topic`` — the recording tool whose Redis
    side effect the suite observes. An ``expr`` maps the scan payload into the tool
    input (without one the scan payload never reaches the tool). ``execution_key`` is
    the key user_id the fire runs as; the admin caller may bind any existing key."""
    body: dict[str, Any] = {
        "name": name,
        "topic": topic,
        "tool": "e2e_record",
        "tool_kwargs": tool_kwargs or {},
        "execution_key": execution_key,
    }
    if expr is not None:
        body["expr"] = expr
    await admin.post("/api/hooks", json=body)


async def mint_link(
    admin: ApiClient,
    topic: str,
    *,
    execution_key: str,
    name: str | None = None,
    ttl_seconds: int | None = None,
    tool_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a trigger link and return its carrier ``{name, trigger_path, token,
    topic, expires_at}`` — the ONLY place the raw token appears. ``ttl_seconds`` is
    the creator's explicit choice (``None`` permanent, positive timed). ``execution_key``
    is the key user_id a fire runs as; the admin caller may bind any existing key."""
    body: dict[str, Any] = {"topic": topic, "ttl_seconds": ttl_seconds, "execution_key": execution_key}
    if name is not None:
        body["name"] = name
    if tool_kwargs is not None:
        body["tool_kwargs"] = tool_kwargs
    return await admin.post("/api/hooks/trigger-links", json=body)


def record_values(stack: TaiStack, key: str) -> list[str]:
    """The ``value`` field each ``e2e_record`` call RPUSH'd under ``key``."""
    return [json.loads(raw)["value"] for raw in stack.records(key)]


def wait_records(stack: TaiStack, key: str, *, count: int, deadline: float = 10.0) -> list[str]:
    """Poll the ``e2e_record`` channel until at least ``count`` values are present,
    then return them — the no-sleep way to observe a background trigger fire."""

    def enough() -> list[str] | None:
        values = record_values(stack, key)
        return values if len(values) >= count else None

    return wait_for(enough, deadline=deadline, message=f"expected >= {count} record(s) under {key!r}")


async def wait_serves_async(stack: TaiStack, name: str, port: int, *, deadline: float = 10.0) -> None:
    """Await until ``name`` is a served MCP tool on ``port`` (the b_serves rebind-wait
    pattern) — fired before minting/firing so a trigger fire (a background dispatch
    with no retry channel) never lands on a not-yet-bound preset."""
    from fastmcp.exceptions import ToolError

    from tai42_e2e import wait_for_async

    async def serves() -> bool:
        try:
            async with stack.mcp(port=port) as mcp:
                return name in await mcp.tool_names()
        except ToolError as exc:
            if "reloading" in str(exc):
                return False
            raise

    await wait_for_async(serves, deadline=deadline, message=f"{name!r} never served on port {port}")
