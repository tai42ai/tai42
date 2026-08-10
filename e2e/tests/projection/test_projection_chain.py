"""The operations→MCP projection chain, end-to-end on a real booted stack.

The management tools PROJECT from the operations layer, gated by the manifest
``api_tools`` block. These specs prove
the whole chain against a live server (not the skeleton's in-process unit tests):

* a destructive projected op carries the ``destructiveHint`` MCP annotation, a
  read op does not;
* ``expose_destructive=false`` drops every destructive op from the surface while
  the reads stay;
* an ``api_tools.include`` naming an unknown op fails startup LOUDLY (no silent
  boot with a missing tool);
* a tier-2 ``/api/auth/*`` op is off the default surface but returns once named in
  ``api_tools.include``;
* a non-privileged key dispatching a projected op over MCP is DENIED at the tool
  edge — a ``PermissionDenied``-backed ``ToolError`` — while a privileged key is
  allowed through the same door.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import pytest
from mcp.types import Tool

from tai42_e2e.manifests import build_projection_stack
from tai42_e2e.stack import TaiStack

# A destructive projected op (POST /api/config/reload → soft-restart) and a read
# op (GET /api/tools) — both project by default; only the first is destructive.
_DESTRUCTIVE_OP = "reload_config"
_READ_OP = "list_tools"
# A tier-2 authority op: every /api/auth/* operation is default-excluded from the
# projected surface, includable only by an explicit api_tools.include.
_TIER2_OP = "list_scopes"


def _by_name(tools: list[Tool], name: str) -> Tool | None:
    return next((t for t in tools if t.name == name), None)


def _is_destructive_hint(tool: Tool) -> bool:
    """Whether the MCP tool advertises ``destructiveHint=True`` — the annotation the
    projection stamps onto a destructive operation (and onto nothing else)."""
    annotations = tool.annotations
    return bool(annotations is not None and annotations.destructiveHint)


def _error_text(result: Any) -> str:
    return json.dumps([getattr(block, "text", "") for block in (result.content or [])]) + str(result)


async def test_destructive_op_projects_with_hint_read_op_without(projection_stack: TaiStack) -> None:
    """The default surface: the destructive op is present AND flagged
    ``destructiveHint``; the read op is present WITHOUT the flag."""
    async with projection_stack.mcp() as mcp:
        tools = await mcp.list_tools()

    destructive = _by_name(tools, _DESTRUCTIVE_OP)
    read = _by_name(tools, _READ_OP)
    assert destructive is not None, f"{_DESTRUCTIVE_OP!r} was not projected onto the MCP surface"
    assert read is not None, f"{_READ_OP!r} was not projected onto the MCP surface"
    assert _is_destructive_hint(destructive), f"{_DESTRUCTIVE_OP!r} must carry destructiveHint=True: {destructive!r}"
    assert not _is_destructive_hint(read), f"the read op {_READ_OP!r} must NOT be flagged destructive: {read!r}"


async def test_tier2_auth_op_default_excluded(projection_stack: TaiStack) -> None:
    """A tier-2 ``/api/auth/*`` op is off the default projected surface even though
    its router is mounted (so the op IS registered)."""
    async with projection_stack.mcp() as mcp:
        names = await mcp.tool_names()
    assert _TIER2_OP not in names, f"tier-2 op {_TIER2_OP!r} must be default-excluded from projection, got: {names}"
    # Guard the test itself: the op's router is mounted, so its sibling reads DID
    # project — otherwise "absent" would be vacuously true.
    assert _READ_OP in names, f"projection surface is unexpectedly empty (no {_READ_OP!r}): {names}"


async def test_expose_destructive_false_drops_destructive_ops(fresh_stack: Callable[..., TaiStack]) -> None:
    """``expose_destructive=false`` makes the destructive ops DISAPPEAR from the
    surface; the reads stay."""
    stack = fresh_stack(partial(build_projection_stack, api_tools={"enabled": True, "expose_destructive": False}))
    async with stack.mcp() as mcp:
        names = await mcp.tool_names()
    assert _DESTRUCTIVE_OP not in names, (
        f"{_DESTRUCTIVE_OP!r} is destructive and must vanish under expose_destructive=false: {names}"
    )
    assert _READ_OP in names, f"the read op {_READ_OP!r} must survive expose_destructive=false: {names}"


async def test_bad_include_fails_boot_loudly(fresh_stack: Callable[..., TaiStack]) -> None:
    """An ``api_tools.include`` naming an unregistered op aborts startup with a loud
    error naming the offending op — never a silent boot with a missing tool."""
    with pytest.raises(RuntimeError, match="totally_unknown_op"):
        fresh_stack(partial(build_projection_stack, api_tools={"enabled": True, "include": ["totally_unknown_op"]}))


async def test_tier2_op_includable(fresh_stack: Callable[..., TaiStack]) -> None:
    """A tier-2 ``/api/auth/*`` op returns to the surface once named in
    ``api_tools.include``."""
    stack = fresh_stack(partial(build_projection_stack, api_tools={"enabled": True, "include": [_TIER2_OP]}))
    async with stack.mcp() as mcp:
        names = await mcp.tool_names()
    assert _TIER2_OP in names, f"tier-2 op {_TIER2_OP!r} must project when explicitly included: {names}"


async def test_authz_denies_underscoped_key_over_mcp(
    projection_authz_stack: tuple[TaiStack, str, str],
) -> None:
    """End-to-end authz through MCP dispatch: a non-privileged key calling a
    projected op is denied at the tool edge (a PermissionDenied-backed ToolError),
    while the privileged key is allowed through the same door."""
    stack, root_token, limited_token = projection_authz_stack

    # The under-scoped key authenticates at /mcp (it carries mcp-access) but lacks
    # the scope the op's synthesized route (/api/tools) requires — the tool-edge
    # authz check denies it.
    async with stack.mcp(auth=limited_token) as mcp:
        denied = await mcp.call_tool(_READ_OP, {}, raise_on_error=False)
    assert denied.is_error, "an under-scoped key must be denied the projected op over MCP"
    assert "access denied" in _error_text(denied).lower(), f"denial was not an authz denial: {_error_text(denied)}"

    # The privileged key ("*") passes the same tool-edge check.
    async with stack.mcp(auth=root_token) as mcp:
        allowed = await mcp.call_tool(_READ_OP, {}, raise_on_error=False)
    assert not allowed.is_error, f"the privileged key must be allowed the projected op: {_error_text(allowed)}"
